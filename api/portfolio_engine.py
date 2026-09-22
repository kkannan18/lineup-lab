import asyncio, hashlib, json, math, re, sqlite3
from collections import Counter, defaultdict

SLEEPER = "https://api.sleeper.app"
CORE_POS = ("QB", "RB", "WR", "TE")
FANTASY_POS = ("QB", "RB", "WR", "TE", "K", "DEF")

def norm(s):
    # Provider names differ on generational suffixes (for example ESPN uses
    # "Kenneth Walker III" while Sleeper uses "Kenneth Walker"). Strip only
    # a terminal suffix before canonicalizing punctuation and whitespace.
    value = (s or "").strip().lower()
    value = re.sub(r"[\s,]+(?:jr|sr|ii|iii|iv|v)\.?$", "", value)
    return re.sub(r"[^a-z0-9]", "", value)

def pct(vals, value):
    if not vals:
        return 50.0
    return 100.0 * sum(v <= value for v in vals) / len(vals)

def score_key(settings):
    rec = float((settings or {}).get("rec", 1) or 0)
    return "pts_ppr" if rec >= .75 else ("pts_half_ppr" if rec >= .25 else "pts_std")

def scoring_signature(settings):
    """Cache metrics by the complete league scoring system, not only PPR tier."""
    encoded = json.dumps(settings or {}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(encoded.encode()).hexdigest()

def fantasy_points(stats, settings):
    """Calculate points from raw Sleeper stats using this league's exact weights."""
    total = 0.0
    matched = 0
    ignored = {"pts_std", "pts_half_ppr", "pts_ppr", "gp"}
    for key, weight in (settings or {}).items():
        if key in ignored or not isinstance(weight, (int, float)):
            continue
        value = stats.get(key)
        if isinstance(value, (int, float)):
            total += float(value) * float(weight)
            matched += 1
    if matched:
        return total
    fallback = score_key(settings)
    return float(stats.get(fallback, stats.get("pts_ppr", 0)) or 0)

def slot_counts(slots):
    return dict(Counter(s for s in slots if s not in ("BN", "IR", "TAXI", "RESERVE")))

def league_format(slots):
    counts = slot_counts(slots)
    sf = counts.get("SUPER_FLEX", 0)
    flex = counts.get("FLEX", 0)
    rec = counts.get("REC_FLEX", 0)
    labels = []
    if sf:
        labels.append(f"{sf} SUPERFLEX" if sf > 1 else "SUPERFLEX")
    if flex:
        labels.append(f"{flex} FLEX" if flex > 1 else "FLEX")
    if rec:
        labels.append(f"{rec} WR/TE FLEX" if rec > 1 else "WR/TE FLEX")
    return " · ".join(labels) or "Standard lineup"

def scoring_summary(settings):
    settings = settings or {}
    rec = float(settings.get("rec", 0) or 0)
    reception = "Full PPR" if rec == 1 else ("Half PPR" if rec == .5 else (f"{rec:g} PPR" if rec else "Standard"))
    pass_td = float(settings.get("pass_td", 4) or 0)
    details = [reception, f"{pass_td:g}-pt pass TD"]
    if settings.get("bonus_rec_te") or settings.get("rec_te"):
        details.append("TE premium")
    if settings.get("bonus_pass_yd_300") or settings.get("bonus_rush_yd_100") or settings.get("bonus_rec_yd_100"):
        details.append("yardage bonuses")
    if float(settings.get("pass_int", -2) or 0) <= -2:
        details.append(f"{float(settings.get('pass_int')):g} INT")
    return " · ".join(details)

def league_configuration(slots, scoring, settings=None, draft=None, acquisition=None, own_roster=None):
    settings = settings or {}
    draft = draft or {}
    acquisition = acquisition or {}
    counts = slot_counts(slots)
    slot_text = " · ".join(f"{count} {slot}" for slot, count in counts.items())
    draft_type = str(draft.get("type") or draft.get("draftType") or "").lower()
    if "auction" in draft_type or "salary" in draft_type:
        draft_label = "Auction draft"
    elif draft_type:
        draft_label = draft_type.replace("_", " ").title() + " draft"
    else:
        draft_label = "Draft format unavailable"
    budget = settings.get("waiver_budget")
    used = ((own_roster or {}).get("settings") or {}).get("waiver_budget_used", 0)
    is_faab = (
        int(settings.get("waiver_type", -1) or -1) == 2
        or bool(acquisition.get("isUsingAcquisitionBudget"))
    )
    if is_faab and budget is not None:
        waiver = f"FAAB ${max(0, int(budget) - int(used or 0))} remaining of ${int(budget)}"
    else:
        waiver = "Traditional waivers"
    position_limits = {}
    for pos in CORE_POS:
        value = settings.get(f"position_limit_{pos.lower()}")
        if isinstance(value, (int, float)) and value > 0:
            position_limits[pos] = int(value)
    best_ball = bool(settings.get("best_ball"))
    keeper_count = int(settings.get("max_keepers", 0) or 0)
    league_type = int(settings.get("type", 0) or 0)
    return {
        "format": league_format(slots),
        "starting_slots": counts,
        "starting_slots_text": slot_text,
        "scoring": scoring_summary(scoring),
        "draft": draft_label,
        "waivers": waiver,
        "is_faab": is_faab,
        "faab_budget": int(budget) if is_faab and budget is not None else None,
        "faab_remaining": max(0, int(budget) - int(used or 0)) if is_faab and budget is not None else None,
        "trade_deadline": settings.get("trade_deadline"),
        "team_count": settings.get("num_teams"),
        "position_limits": position_limits,
        "best_ball": best_ball,
        "keeper_count": keeper_count,
        "league_type": "Dynasty" if league_type == 2 else ("Keeper" if keeper_count else "Redraft"),
    }

def usage(stats, pos):
    if pos == "QB":
        return float(stats.get("pass_att", 0) or 0) + 2 * float(stats.get("rush_att", 0) or 0)
    if pos == "RB":
        return float(stats.get("rush_att", 0) or 0) + float(stats.get("rec_tgt", 0) or 0)
    if pos in ("WR", "TE"):
        return float(stats.get("rec_tgt", 0) or 0) + float(stats.get("rush_att", 0) or 0)
    if pos == "K":
        return float(stats.get("fga", 0) or 0) + float(stats.get("xpa", 0) or 0)
    return float(stats.get("off_snp", 0) or 0)

def eligible(pos, slot):
    if pos == slot:
        return True
    if slot in ("DST", "D/ST") and pos == "DEF":
        return True
    if slot == "FLEX":
        return pos in ("RB", "WR", "TE")
    if slot == "SUPER_FLEX":
        return pos in ("QB", "RB", "WR", "TE")
    if slot == "REC_FLEX":
        return pos in ("WR", "TE")
    if slot in ("WRRB_FLEX", "RB_WR_FLEX"):
        return pos in ("RB", "WR")
    return False

def assign(players, slots):
    """Maximum-value legal lineup using a slot-mask dynamic program.

    Runtime scales with the number of starting slots, not roster size. This
    handles repeated FLEX/SUPERFLEX slots exactly without the exponential
    player-mask cost of the previous implementation.
    """
    open_slots = [s for s in slots if s not in ("BN", "IR", "TAXI", "RESERVE")]
    # Most restrictive slots first keeps equivalent states deterministic.
    open_slots.sort(key=lambda slot: (
        sum(eligible(p["position"], slot) for p in players),
        slot,
    ))
    # mask -> (value, ((player_id, slot), ...))
    states = {0: (0.0, ())}
    for player in players:
        prior = list(states.items())
        for mask, (value, choices) in prior:
            for slot_index, slot in enumerate(open_slots):
                bit = 1 << slot_index
                if mask & bit or not eligible(player["position"], slot):
                    continue
                next_mask = mask | bit
                next_value = value + float(
                    player.get("lineup_value", player.get("score", 0)) or 0
                )
                current = states.get(next_mask)
                if current is None or next_value > current[0]:
                    states[next_mask] = (
                        next_value,
                        choices + ((player["id"], slot),),
                    )
    # Prefer filling the most slots, then maximize expected value.
    _, (_, choices) = max(
        states.items(),
        key=lambda item: (item[0].bit_count(), item[1][0]),
    )
    return dict(choices)

def lineup_value(ids, metrics, slots):
    players = [dict(metrics[x]) for x in ids if x in metrics]
    selected = assign(players, slots)
    return sum(float(metrics[pid].get("lineup_value", 0) or 0) for pid in selected)

def effective_starter_counts(all_teams, metrics, slots):
    totals = Counter()
    teams = 0
    for ids in all_teams.values():
        chosen = assign([dict(metrics[x]) for x in ids if x in metrics], slots)
        if not chosen:
            continue
        teams += 1
        for pid in chosen:
            totals[metrics[pid]["position"]] += 1
    return {
        pos: max(1, int(round(totals[pos] / max(1, teams))))
        for pos in CORE_POS
    }

async def make_feed(get_json, season, week):
    prior = list(range(max(1, week - 4), week))
    calls = [
        get_json(f"{SLEEPER}/v1/players/nfl", ttl=3600),
        get_json(f"{SLEEPER}/projections/nfl/{season}/{week}?season_type=regular", ttl=300),
        get_json("https://site.api.espn.com/apis/site/v2/sports/football/nfl/injuries", ttl=300),
        get_json(f"https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard?dates={season}&seasontype=2&week={week}&limit=100", ttl=300),
    ]
    calls += [get_json(f"{SLEEPER}/stats/nfl/{season}/{w}?season_type=regular", ttl=1800) for w in prior]
    players, projections, injury_payload, scoreboard, *history = await asyncio.gather(*calls)
    projection = {str(x.get("player_id")): x for x in projections if x.get("player_id")}
    games_by_team = {}
    for event in scoreboard.get("events", []):
        competition = (event.get("competitions") or [{}])[0]
        for side in competition.get("competitors", []):
            abbr = ((side.get("team") or {}).get("abbreviation") or "").upper()
            if abbr:
                games_by_team[abbr] = {
                    "kickoff": event.get("date"),
                    "game": event.get("name"),
                    "game_status": ((event.get("status") or {}).get("type") or {}).get("state"),
                }
    hist = defaultdict(list)
    history_by_week = defaultdict(list)
    defense = defaultdict(list)
    for history_week, rows in zip(prior, history):
        seen = set()
        for x in rows:
            pid = str(x.get("player_id"))
            st = x.get("stats") or {}
            meta = players.get(pid) or {}
            pos = meta.get("position") or ((meta.get("fantasy_positions") or [""])[0])
            hist[pid].append(st)
            history_by_week[pid].append({"week": history_week, "stats": st})
            seen.add(pid)
            if x.get("opponent") and pos in CORE_POS:
                defense[(x["opponent"], pos)].append(st)
    # ESPN IDs differ from Sleeper IDs. Match by normalized name + position,
    # preferring active players with a team and a current projection to avoid
    # collisions with historical players who share a name.
    candidates = defaultdict(list)
    for pid, p in players.items():
        if not p.get("full_name"):
            continue
        pos = p.get("position") or ((p.get("fantasy_positions") or [""])[0])
        rank = (bool(p.get("active")), bool(p.get("team")), str(pid) in projection)
        candidates[(norm(p["full_name"]), pos)].append((rank, str(pid)))
    by_name_pos = {k: max(v)[1] for k, v in candidates.items()}
    return {
        "players": players, "projection": projection, "hist": hist,
        "history_by_week": history_by_week,
        "defense": defense, "prior_count": max(1, len(prior)),
        "by_name_pos": by_name_pos, "metric_sets": {},
        "injury_payload": injury_payload, "games_by_team": games_by_team
    }

def consistency_metrics(feed, pid, pos, settings):
    """Reward repeatable floors while keeping one-game samples neutral.

    A week qualifies when the player scores 10+ fantasy points OR reaches a
    position-aware opportunity floor. The score combines repeat hit rate and
    the current consecutive streak across up to four completed weeks.
    """
    thresholds = {"QB": 30, "RB": 10, "WR": 6, "TE": 5, "K": 5, "DEF": 1}
    samples = []
    for item in feed.get("history_by_week", {}).get(pid, []):
        stats = item["stats"]
        points = fantasy_points(stats, settings)
        opportunities = usage(stats, pos)
        hit = points >= 10 or opportunities >= thresholds.get(pos, 10)
        samples.append({
            "week": item["week"],
            "points": round(points, 1),
            "opportunities": round(opportunities, 1),
            "hit": hit,
        })

    n = len(samples)
    hits = sum(x["hit"] for x in samples)
    streak = 0
    for sample in reversed(samples):
        if not sample["hit"]:
            break
        streak += 1

    if n < 2:
        # Neutral in the model: one result is evidence, not consistency.
        score = 50.0
        label = "Insufficient sample"
    else:
        hit_rate = hits / n
        streak_component = min(streak, 3) / min(n, 3)
        sample_reliability = min(1.0, (n - 1) / 3)
        score = 100 * (.60 * hit_rate + .30 * streak_component + .10 * sample_reliability)
        label = "Reliable" if score >= 75 else ("Mixed" if score >= 45 else "Volatile")

    return {
        "consistency": round(score, 1),
        "consistency_label": label,
        "consistency_samples": n,
        "consistency_hits": hits,
        "consistency_hit_rate": round(100 * hits / n) if n else 0,
        "consistency_streak": streak,
        "weekly_form": samples,
        "consistency_threshold": f"10+ points or {thresholds.get(pos, 10)}+ opportunities",
    }

def metric_set(feed, settings):
    key = score_key(settings)
    cache_key = scoring_signature(settings)
    if cache_key in feed["metric_sets"]:
        return feed["metric_sets"][cache_key]
    raw = {}
    defense_allowed = defaultdict(list)
    for (opp, pos), games in feed["defense"].items():
        values = [fantasy_points(g, settings) for g in games]
        if values:
            defense_allowed[pos].append(sum(values) / feed["prior_count"])
    for pid, pr in feed["projection"].items():
        meta = feed["players"].get(pid) or {}
        pos = meta.get("position") or ((meta.get("fantasy_positions") or [""])[0])
        if pos not in FANTASY_POS or not meta.get("active") or not meta.get("team"):
            continue
        ps = pr.get("stats") or {}
        projected = fantasy_points(ps, settings)
        games = feed["hist"].get(pid, [])
        recent = sum(fantasy_points(g, settings) for g in games) / feed["prior_count"]
        involved = sum(usage(g, pos) for g in games) / feed["prior_count"]
        opp = pr.get("opponent")
        dgames = feed["defense"].get((opp, pos), [])
        allowed = None
        if dgames:
            allowed = sum(fantasy_points(g, settings) for g in dgames) / feed["prior_count"]
        raw[pid] = {
            "id": pid, "name": meta.get("full_name") or pid, "position": pos,
            **consistency_metrics(feed, pid, pos, settings),
            "team": meta.get("team") or "FA", "opponent": opp or "TBD",
            "projected": projected, "recent": recent, "usage": involved,
            "allowed": allowed, "injury": meta.get("injury_status"),
            **feed.get("games_by_team", {}).get((meta.get("team") or "").upper(), {})
        }
    # Injured starters can disappear from a current-week projection feed entirely.
    # Add those unavailable players back as context so their vacated role can boost
    # healthy backups at the same team/position (and so rostered injured players
    # remain visible). Historical usage/scoring estimates the size of the missing role.
    from .injury_engine import parse_injuries
    injuries = parse_injuries(feed.get("injury_payload") or {})
    for identity, report in injuries.items():
        status = (report.get("status") or "").lower()
        if not any(token in status for token in ("questionable", "doubtful", "out", "inactive", "injured reserve", "ir", "suspend")):
            continue
        pid = feed.get("by_name_pos", {}).get(identity)
        if not pid or pid in raw:
            continue
        meta = feed["players"].get(pid) or {}
        pos = identity[1]
        games = feed["hist"].get(pid, [])
        recent = sum(fantasy_points(g, settings) for g in games) / feed["prior_count"]
        involved = sum(usage(g, pos) for g in games) / feed["prior_count"]
        team = (report.get("team") or meta.get("team") or "").upper()
        if not team:
            continue
        raw[pid] = {
            "id": pid, "name": meta.get("full_name") or report.get("name") or pid,
            "position": pos, "team": team, "opponent": "TBD",
            **consistency_metrics(feed, pid, pos, settings),
            "projected": 0.0, "recent": recent, "usage": involved,
            "allowed": None, "injury": meta.get("injury_status"),
            **feed.get("games_by_team", {}).get(team, {})
        }

    groups = defaultdict(lambda: defaultdict(list))
    for p in raw.values():
        groups[p["position"]]["projected"].append(p["projected"])
        groups[p["position"]]["recent"].append(p["recent"])
        groups[p["position"]]["usage"].append(p["usage"])
    for p in raw.values():
        g = groups[p["position"]]
        matchup = pct(defense_allowed[p["position"]], p["allowed"]) if p["allowed"] is not None else 50
        pp, rp, up = pct(g["projected"], p["projected"]), pct(g["recent"], p["recent"]), pct(g["usage"], p["usage"])
        p["matchup"] = round(matchup)
        p["projection_signal"] = round(pp)
        p["recent_signal"] = round(rp)
        p["involvement_signal"] = round(up)
        p["projected"], p["recent"], p["usage"] = round(p["projected"], 1), round(p["recent"], 1), round(p["usage"], 1)
    from .injury_engine import enrich_metrics, apply_teammate_opportunity
    enrich_metrics(raw, injuries)
    apply_teammate_opportunity(raw)
    for p in raw.values():
        core = (
            .35 * p["projection_signal"]
            + .20 * p["recent_signal"]
            + .15 * p["involvement_signal"]
            + .12 * p["matchup"]
            + .18 * p["consistency"]
        )
        boosted = min(100, core + p["opportunity_bonus"])
        p["score_before_injury"] = round(boosted, 1)
        p["score"] = round(boosted * (1 - float(p.get("risk") or 0)), 1)

        # Cross-position expected value for starter/FLEX optimization.
        # Projection and recent points share the same fantasy-point scale.
        # Involvement, matchup, and consistency are bounded modifiers rather
        # than position-relative scores being compared directly.
        involvement_factor = .90 + .20 * (p["involvement_signal"] / 100)
        matchup_factor = .90 + .20 * (p["matchup"] / 100)
        consistency_factor = .95 + .10 * (p["consistency"] / 100)
        base_points = .68 * p["projected"] + .22 * p["recent"] + .10 * p["projected"] * involvement_factor
        role_points = p["projected"] * (p.get("teammate_opportunity", 0) / 100) * .15
        p["lineup_value_before_injury"] = round(
            (base_points * matchup_factor * consistency_factor) + role_points, 2
        )
        p["lineup_value"] = round(
            p["lineup_value_before_injury"] * (1 - float(p.get("risk") or 0)), 2
        )
    feed["metric_sets"][cache_key] = raw
    return raw

def starter_counts(slots):
    c = Counter(s for s in slots if s in CORE_POS)
    return {p: max(1, c[p]) for p in CORE_POS}

def team_rating(ids, metrics, counts, pos):
    vals = sorted((metrics[x]["score"] for x in ids if x in metrics and metrics[x]["position"] == pos), reverse=True)
    n = counts[pos]
    return round(sum(vals[:n]) / n, 1) if vals else 0

def position_profile(own_ids, all_teams, metrics, slots):
    counts = effective_starter_counts(all_teams, metrics, slots)
    result = []
    for pos in CORE_POS:
        ratings = [team_rating(ids, metrics, counts, pos) for ids in all_teams.values()]
        mine = team_rating(own_ids, metrics, counts, pos)
        rank = 1 + sum(x > mine for x in ratings)
        strength = round(100 * sum(x <= mine for x in ratings) / max(1, len(ratings)))
        label = "Strength" if strength >= 67 else ("Need" if strength <= 40 else "Average")
        result.append({"position": pos, "rating": mine, "percentile": strength, "rank": rank,
                       "teams": len(ratings), "label": label})
    return result

def decorate_lineup(own_ids, current_ids, metrics, slots):
    players = [dict(metrics[x]) for x in own_ids if x in metrics]
    recommended = assign(players, slots)
    current = set(current_ids)
    for p in players:
        p["recommended_slot"] = recommended.get(p["id"], "Bench")
        p["recommendation"] = "Start" if p["id"] in recommended else "Bench"
        p["current"] = "Starter" if p["id"] in current else "Bench"
        match = "favorable" if p["matchup"] >= 67 else ("tough" if p["matchup"] <= 33 else "neutral")
        opportunity = ""
        if p.get("teammate_opportunity", 0) > 0:
            names = ", ".join(x["name"] for x in p.get("injured_position_mates", [])[:2])
            opportunity = f" · +{p['opportunity_bonus']} role boost from {names}"
        consistency = (
            f"{p['consistency_streak']}-week qualifying streak"
            if p["consistency_samples"] >= 2
            else "consistency sample still limited"
        )
        p["reason"] = f"{p['projected']} projected · {p['recent']} recent avg · {p['usage']} opportunities/game · {consistency} · {match} matchup · {p['lineup_value']} expected lineup value{opportunity}"
    players.sort(key=lambda p: (p["recommendation"] != "Start", -p["score"]))
    changes = sum(p["current"] == "Starter" and p["recommendation"] == "Bench" for p in players)
    return players, changes

def market_label(score):
    if score >= 85: return "Premium"
    if score >= 70: return "Strong"
    if score >= 55: return "Useful"
    return "Depth"

def recommendations(own_ids, current_ids, all_teams, team_names, metrics, slots, profile, owned_names=None, league_config=None):
    league_config = league_config or {}
    position_limits = league_config.get("position_limits") or {}
    owned = {str(x) for ids in all_teams.values() for x in ids}
    owned_names = {norm(x) for x in (owned_names or set()) if x}
    needs = {p["position"]: max(0, 65 - p["percentile"]) for p in profile}
    counts = effective_starter_counts(all_teams, metrics, slots)
    baseline_lineup_value = lineup_value(own_ids, metrics, slots)
    own_by_pos = defaultdict(list)
    for pid in own_ids:
        if pid in metrics:
            own_by_pos[metrics[pid]["position"]].append(metrics[pid])
    for vals in own_by_pos.values():
        vals.sort(key=lambda x: x["score"], reverse=True)

    # Exact marginal-lineup evaluation is intentionally limited to a strong
    # prefiltered pool. Running the legal-lineup optimizer for every NFL player
    # is expensive on a serverless request and adds no useful waiver signal.
    waiver_pool = []
    for p in metrics.values():
        # Waiver eligibility is a hard prerequisite, never a ranking feature.
        # Check both the provider/player ID and canonicalized display name so
        # an ESPN-to-Sleeper identity miss cannot leak a rostered player.
        if (
            str(p["id"]) in owned
            or norm(p.get("name")) in owned_names
            or p["position"] not in CORE_POS
            or p["projected"] < 1
        ):
            continue
        need = needs.get(p["position"], 0)
        prefilter_score = (
            float(p.get("score") or 0)
            + .25 * need
            + 2 * float(p.get("projected") or 0)
        )
        waiver_pool.append((prefilter_score, p))
    waiver_pool.sort(key=lambda item: -item[0])

    # Preserve positional diversity while capping exact evaluations.
    candidates, candidate_counts = [], Counter()
    for _, p in waiver_pool:
        if candidate_counts[p["position"]] >= 18:
            continue
        candidates.append(p)
        candidate_counts[p["position"]] += 1
        if len(candidates) >= 72:
            break

    waiver = []
    for p in candidates:
        pos = p["position"]
        floor = own_by_pos[pos][-1]["score"] if own_by_pos[pos] else 0
        improvement = round(p["score"] - floor, 1)
        need = needs.get(pos, 0)
        best_gain, drop = 0.0, None
        current_pos_count = len(own_by_pos[pos])
        limit = position_limits.get(pos)
        for owned_id in own_ids:
            drop_pos = (metrics.get(owned_id) or {}).get("position")
            if limit and current_pos_count >= limit and drop_pos != pos:
                continue
            trial = [x for x in own_ids if x != owned_id] + [p["id"]]
            gain = lineup_value(trial, metrics, slots) - baseline_lineup_value
            if gain > best_gain:
                best_gain = gain
                drop = metrics.get(owned_id, {}).get("name")
        lineup_gain = round(best_gain, 2)
        priority = p["score"] + .35 * need + max(0, improvement) * .1 + 4 * max(0, lineup_gain)
        faab_pct = min(35, max(1, round(2 + max(0, lineup_gain) * 2 + max(0, need) * .08)))
        waiver.append({**p, "priority": round(priority, 1), "improvement": improvement,
                       "lineup_gain": lineup_gain, "drop": drop,
                       "faab_pct": faab_pct, "availability_verified": True,
                       "availability_label": "Verified unrostered",
                       "why": f"{pos} is {next((x['label'].lower() for x in profile if x['position']==pos),'a need')}; "
                       f"{p['projected']} projected with {p['usage']} recent opportunities/game; "
                       f"{lineup_gain:+.2f} marginal lineup value in this {league_config.get('format', 'league')} format"
                       + (f"; +{p['opportunity_bonus']} role boost with {', '.join(x['name'] for x in p.get('injured_position_mates', [])[:2])} limited" if p.get("opportunity_bonus", 0) else "")})
    waiver.sort(key=lambda x: -x["priority"])
    # Diversify the list while preserving priority.
    selected, per_pos = [], Counter()
    for p in waiver:
        if per_pos[p["position"]] < 2:
            selected.append(p); per_pos[p["position"]] += 1
        if len(selected) == 7: break

    trade_targets = []
    for owner, ids in all_teams.items():
        if owner == "mine": continue
        by_pos = defaultdict(list)
        for pid in ids:
            if pid in metrics:
                by_pos[metrics[pid]["position"]].append(metrics[pid])
        for vals in by_pos.values():
            vals.sort(key=lambda x: x["score"], reverse=True)
        for pid in ids:
            if pid not in metrics: continue
            p = metrics[pid]
            if p["position"] not in CORE_POS or p["score"] < 48: continue
            depth = by_pos[p["position"]]
            owner_rank = 1 + next((i for i, x in enumerate(depth) if x["id"] == pid), len(depth))
            surplus = max(0, len(depth) - counts[p["position"]])
            expendable = owner_rank > counts[p["position"]]
            need = needs.get(p["position"], 0)
            legal_gains = []
            current_pos_count = len(own_by_pos[p["position"]])
            limit = position_limits.get(p["position"])
            for owned_id in own_ids:
                drop_pos = (metrics.get(owned_id) or {}).get("position")
                if limit and current_pos_count >= limit and drop_pos != p["position"]:
                    continue
                trial = [x for x in own_ids if x != owned_id] + [p["id"]]
                legal_gains.append(lineup_value(trial, metrics, slots) - baseline_lineup_value)
            lineup_gain = max([0.0] + legal_gains)
            priority = .55 * p["score"] + .55 * need + 5 * lineup_gain + (14 if expendable else 0) + 2 * surplus
            attainability = "More attainable" if expendable else ("Reach target" if owner_rank == 1 else "Core starter")
            trade_targets.append({**p, "owner": team_names.get(owner, "League mate"),
              "priority": round(priority, 1), "market": market_label(p["score"]),
              "lineup_gain": round(lineup_gain, 2),
              "attainability": attainability,
              "why": f"{lineup_gain:+.2f} potential lineup value in this {league_config.get('format', 'league')} format; "
                     f"{team_names.get(owner,'that manager')} rosters {len(depth)} at {p['position']}"
                     f" and this player ranks #{owner_rank} for them"})
    trade_targets.sort(key=lambda x: -x["priority"])
    selected_targets, target_pos = [], Counter()
    for p in trade_targets:
        if target_pos[p["position"]] < 2:
            selected_targets.append(p); target_pos[p["position"]] += 1
        if len(selected_targets) == 7: break

    starters = set(current_ids)
    strengths = {x["position"] for x in profile if x["label"] == "Strength"}
    trade_away = []
    for pid in own_ids:
        if pid not in metrics: continue
        p = metrics[pid]
        if p["position"] not in CORE_POS or p["score"] < 35: continue
        depth = own_by_pos[p["position"]]
        rank = 1 + next((i for i, x in enumerate(depth) if x["id"] == pid), len(depth))
        surplus = max(0, len(depth) - counts[p["position"]])
        bench = pid not in starters
        # Protect cornerstone starters; surface useful depth and secondary starters from strengths.
        if rank == 1 or (not bench and p["position"] not in strengths):
            continue
        without_value = lineup_value([x for x in own_ids if x != pid], metrics, slots)
        lineup_loss = max(0, baseline_lineup_value - without_value)
        priority = p["score"] + (22 if bench else 0) + (10 if p["position"] in strengths else 0) + 3 * surplus - 4 * rank - 6 * lineup_loss
        trade_away.append({**p, "priority": round(priority, 1), "market": market_label(p["score"]),
          "lineup_loss": round(lineup_loss, 2),
          "why": f"{lineup_loss:.2f} expected lineup value lost if moved in this {league_config.get('format', 'league')} format; "
                 f"you roster {len(depth)} {p['position']}s and this player ranks #{rank} for you"})
    trade_away.sort(key=lambda x: -x["priority"])
    return selected, selected_targets, trade_away[:6]

def summarize(team):
    need = [p["position"] for p in team["positions"] if p["label"] == "Need"]
    strong = [p["position"] for p in team["positions"] if p["label"] == "Strength"]
    team["need_text"] = ", ".join(need) if need else "No major positional hole"
    team["strength_text"] = ", ".join(strong) if strong else "Balanced roster"
    alerts = []
    for p in team["lineup"]:
        report = p.get("injury_report")
        status = (p.get("availability") or "").lower()
        practice = (report or {}).get("practice") or ""
        if not report or (status in ("active", "no current designation") and not practice):
            continue
        urgency = 3 if any(x in status for x in ("out", "inactive", "injured reserve")) else (
            2 if "doubtful" in status else (1 if "questionable" in status else 0)
        )
        alerts.append({
            "name": p["name"], "position": p["position"], "team": p["team"],
            "current": p["current"], "recommendation": p["recommendation"],
            "status": p.get("availability"), "injury_type": report.get("injury_type"),
            "practice": practice, "update": report.get("update"),
            "updated_at": report.get("updated_at"), "source": report.get("source"),
            "source_url": report.get("source_url"), "kickoff": p.get("kickoff"),
            "game": p.get("game"), "urgency": urgency,
        })
    alerts.sort(key=lambda x: (-x["urgency"], x["kickoff"] or "9999", x["name"]))
    team["alerts"] = alerts
    team["urgent_alerts"] = sum(x["urgency"] >= 1 for x in alerts)
    return team

async def sleeper_team(get_json, league, user_id, feed, week):
    lid = league["league_id"]
    draft_id = league.get("draft_id")
    calls = [
        get_json(f"{SLEEPER}/v1/league/{lid}/rosters", ttl=30),
        get_json(f"{SLEEPER}/v1/league/{lid}/users", ttl=300),
    ]
    if draft_id:
        calls.append(get_json(f"{SLEEPER}/v1/draft/{draft_id}", ttl=3600))
    fetched = await asyncio.gather(*calls)
    rosters, users = fetched[:2]
    draft = fetched[2] if len(fetched) > 2 else {}
    own = next((r for r in rosters if str(r.get("owner_id")) == str(user_id)), None)
    if not own or not own.get("players"):
        return None
    names = {}
    for u in users:
        meta = u.get("metadata") or {}
        names[str(u.get("user_id"))] = meta.get("team_name") or u.get("display_name") or "League mate"
    all_teams, team_names, owned_names = {}, {}, set()
    for r in rosters:
        owner = "mine" if r is own else str(r.get("owner_id") or r.get("roster_id"))
        roster_ids = [str(x) for x in (r.get("players") or [])]
        all_teams[owner] = roster_ids
        for pid in roster_ids:
            meta = feed["players"].get(pid) or {}
            if meta.get("full_name"):
                owned_names.add(meta["full_name"])
        team_names[owner] = names.get(str(r.get("owner_id")), f"Team {r.get('roster_id')}")
    metrics = metric_set(feed, league.get("scoring_settings") or {})
    own_ids = all_teams["mine"]
    current = [str(x) for x in (own.get("starters") or [])]
    slots = league.get("roster_positions") or []
    league_config = league_configuration(
        slots, league.get("scoring_settings") or {},
        league.get("settings") or {}, draft=draft, own_roster=own
    )
    profile = position_profile(own_ids, all_teams, metrics, slots)
    lineup, changes = decorate_lineup(own_ids, current, metrics, slots)
    if league_config.get("best_ball"):
        changes = 0
    waiver, targets, away = recommendations(
        own_ids, current, all_teams, team_names, metrics, slots, profile,
        owned_names, league_config
    )
    return summarize({
        "id": f"sleeper:{lid}", "platform": "Sleeper", "league_id": lid,
        "league": league.get("name") or "Sleeper league", "team": names.get(str(user_id), "My team"),
        "positions": profile, "lineup": lineup, "changes": changes,
        "league_config": league_config,
        "waivers": waiver, "trade_targets": targets, "trade_away": away
    })

ESPN_SLOT = {0:"QB", 2:"RB", 4:"WR", 6:"TE", 16:"DEF", 17:"K", 20:"BN", 21:"IR", 23:"FLEX", 7:"SUPER_FLEX"}
ESPN_STAT_TO_SLEEPER = {
    3:"pass_yd", 4:"pass_td", 19:"pass_2pt", 20:"pass_int",
    24:"rush_yd", 25:"rush_td", 26:"rush_2pt",
    42:"rec_yd", 43:"rec_td", 44:"rec_2pt", 53:"rec", 72:"fum_lost",
    77:"fgm", 80:"xpm", 85:"fgmiss", 86:"xpmiss",
}

def espn_scoring(settings):
    items = ((settings.get("scoringSettings") or {}).get("scoringItems") or [])
    result = {}
    for item in items:
        key = ESPN_STAT_TO_SLEEPER.get(int(item.get("statId", -1)))
        if key:
            result[key] = float(item.get("points", 0) or 0)
    # Preserve a sensible fallback when an ESPN scoring item is unavailable.
    result.setdefault("rec", 1 if (settings.get("scoringSettings") or {}).get("playerRankType") == "PPR" else 0)
    result.setdefault("pass_td", 4)
    result.setdefault("pass_yd", .04)
    result.setdefault("rush_yd", .1)
    result.setdefault("rush_td", 6)
    result.setdefault("rec_yd", .1)
    result.setdefault("rec_td", 6)
    return result

def espn_to_sleeper(entry, feed):
    p = ((entry.get("playerPoolEntry") or {}).get("player") or {})
    pos = {1:"QB", 2:"RB", 3:"WR", 4:"TE", 5:"K", 16:"DEF"}.get(p.get("defaultPositionId"))
    return feed["by_name_pos"].get((norm(p.get("fullName")), pos))

async def espn_team(fetch_espn, league_id, season, secret, label, feed, week):
    league = await fetch_espn(league_id, season, secret, week)
    requested_team_id = secret.get("team_id")
    if requested_team_id is not None:
        own = next(
            (t for t in league.get("teams", [])
             if str(t.get("id")) == str(requested_team_id)),
            None,
        )
    else:
        swid = norm(secret.get("swid"))
        own = next(
            (t for t in league.get("teams", [])
             if any(norm(x) == swid for x in (t.get("owners") or []))),
            None,
        )
    if not own:
        return None, "Choose a valid team from this ESPN league"
    all_teams, team_names, current, owned_names = {}, {}, [], set()
    for t in league.get("teams", []):
        key = "mine" if t is own else str(t.get("id"))
        team_names[key] = t.get("name") or f"Team {t.get('id')}"
        ids = []
        for e in ((t.get("roster") or {}).get("entries") or []):
            espn_player = ((e.get("playerPoolEntry") or {}).get("player") or {})
            if espn_player.get("fullName"):
                owned_names.add(espn_player["fullName"])
            pid = espn_to_sleeper(e, feed)
            if pid:
                ids.append(pid)
                if t is own and int(e.get("lineupSlotId", 20)) not in (20, 21):
                    current.append(pid)
        all_teams[key] = ids
    lineup_counts = (((league.get("settings") or {}).get("rosterSettings") or {}).get("lineupSlotCounts") or {})
    slots = []
    for sid, count in lineup_counts.items():
        slots += [ESPN_SLOT.get(int(sid), "BN")] * int(count)
    espn_settings = league.get("settings") or {}
    scoring = espn_scoring(espn_settings)
    metrics = metric_set(feed, scoring)
    own_ids = all_teams["mine"]
    acquisition = espn_settings.get("acquisitionSettings") or {}
    roster_settings = espn_settings.get("rosterSettings") or {}
    position_id_to_name = {1:"QB", 2:"RB", 3:"WR", 4:"TE"}
    espn_limits = {
        position_id_to_name[int(pid)]: int(limit)
        for pid, limit in (roster_settings.get("positionLimits") or {}).items()
        if int(pid) in position_id_to_name and isinstance(limit, (int, float)) and limit > 0
    }
    config_settings = {"num_teams": len(league.get("teams", []))}
    league_config = league_configuration(
        slots, scoring, settings=config_settings,
        draft=espn_settings.get("draftSettings") or {},
        acquisition=acquisition, own_roster=own,
    )
    league_config["position_limits"] = espn_limits
    profile = position_profile(own_ids, all_teams, metrics, slots)
    lineup, changes = decorate_lineup(own_ids, current, metrics, slots)
    waiver, targets, away = recommendations(
        own_ids, current, all_teams, team_names, metrics, slots, profile,
        owned_names, league_config
    )
    return summarize({
        "id": f"espn:{season}:{league_id}", "platform": "ESPN", "league_id": str(league_id),
        "league": label, "team": team_names["mine"], "positions": profile,
        "league_config": league_config,
        "lineup": lineup, "changes": changes, "waivers": waiver,
        "trade_targets": targets, "trade_away": away
    }), None

async def build_portfolio(get_json, fetch_espn, connections, season, week):
    feed = await make_feed(get_json, season, week)
    teams, skipped = [], []
    sleeper_connections = [c for c in connections if c["platform"] == "sleeper"]
    for conn in sleeper_connections:
        leagues = await get_json(f"{SLEEPER}/v1/user/{conn['account_key']}/leagues/nfl/{season}", ttl=300)
        active = [x for x in leagues if x.get("status") in ("in_season", "post_season")]
        results = await asyncio.gather(*(sleeper_team(get_json, x, conn["account_key"], feed, week) for x in active),
                                       return_exceptions=True)
        for league, result in zip(active, results):
            if isinstance(result, Exception):
                skipped.append({"league": league.get("name"), "reason": str(result)})
            elif result:
                teams.append(result)
    for conn in [c for c in connections if c["platform"] == "espn"]:
        try:
            conn_season, lid = conn["account_key"].split(":", 1)
            if int(conn_season) != int(season):
                continue
            team, reason = await espn_team(fetch_espn, lid, season, json.loads(conn["secret_json"]), conn["label"], feed, week)
            if team: teams.append(team)
            elif reason: skipped.append({"league": conn["label"], "reason": reason})
        except Exception as e:
            skipped.append({"league": conn["label"], "reason": str(e)})
    teams.sort(key=lambda x: (x["platform"], x["league"]))
    return {
        "season": season, "week": week, "teams": teams, "skipped": skipped,
        "summary": {
            "leagues": len(teams),
            "lineup_changes": sum(x["changes"] for x in teams),
            "waiver_targets": sum(len(x["waivers"]) for x in teams),
            "trade_targets": sum(len(x["trade_targets"]) for x in teams),
            "injury_alerts": sum(x.get("urgent_alerts", 0) for x in teams)
        },
        "method": "35% projection · 20% recent scoring · 15% involvement · 12% matchup · 18% consistency · up to +10 teammate-injury opportunity"
    }
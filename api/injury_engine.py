import re
from datetime import datetime, timezone

CORE_POS = {"QB", "RB", "WR", "TE", "K"}
STATUS_RISK = {
    "out": 1.0,
    "inactive": 1.0,
    "injured reserve": 1.0,
    "ir": 1.0,
    "suspended": 1.0,
    "doubtful": 0.72,
    "questionable": 0.22,
    "probable": 0.05,
    "active": 0.0,
}

def norm(value):
    return re.sub(r"[^a-z0-9]", "", (value or "").lower().replace("jr.", "").replace("sr.", ""))

def _practice_summary(text):
    text = text or ""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    signals = (
        "practice", "limited participant", "full participant",
        "did not participate", "DNP", "LP/", "FP/"
    )
    selected = [s.strip() for s in sentences if any(x.lower() in s.lower() for x in signals)]
    return " ".join(selected[:2])[:450]

def parse_injuries(payload):
    result = {}
    for club in payload.get("injuries", []):
        for item in club.get("injuries", []):
            athlete = item.get("athlete") or {}
            position = ((athlete.get("position") or {}).get("abbreviation") or "").upper()
            if position not in CORE_POS:
                continue
            name = athlete.get("displayName")
            if not name:
                continue
            detail = item.get("details") or {}
            fantasy = detail.get("fantasyStatus") or {}
            direct_status = item.get("status")
            if isinstance(direct_status, dict):
                direct_status = direct_status.get("type") or direct_status.get("name")
            notes = ((item.get("notes") or {}).get("items") or [])
            latest_note = notes[0] if notes else {}
            status = (
                fantasy.get("description")
                or direct_status
                or ((item.get("type") or {}).get("description"))
                or "Unspecified"
            ).replace("_", " ").title()
            comment = (
                item.get("longComment") or item.get("shortComment")
                or latest_note.get("text") or latest_note.get("headline") or ""
            )
            links = athlete.get("links") or []
            source_url = next(
                (x.get("href") for x in links if "news" in (x.get("rel") or [])),
                next((x.get("href") for x in links if "playercard" in (x.get("rel") or [])), None),
            )
            raw_date = item.get("date")
            result[(norm(name), position)] = {
                "status": status,
                "injury_type": detail.get("type") or "",
                "team": ((athlete.get("team") or {}).get("abbreviation") or "").upper(),
                "practice": _practice_summary(comment),
                "update": (item.get("shortComment") or latest_note.get("headline") or comment)[:600],
                "updated_at": latest_note.get("date") or raw_date,
                "source": "ESPN injury report / RotoWire",
                "source_url": source_url,
            }
    return result

def enrich_metrics(metrics, injuries):
    now = datetime.now(timezone.utc)
    for player in metrics.values():
        report = injuries.get((norm(player["name"]), player["position"]))
        sleeper_status = player.get("injury")
        if report:
            status = report["status"]
            player["injury_report"] = report
        elif sleeper_status:
            status = str(sleeper_status).replace("_", " ").title()
            player["injury_report"] = {
                "status": status,
                "injury_type": "",
                "practice": "",
                "update": "Sleeper currently lists this player with an injury designation.",
                "updated_at": None,
                "source": "Sleeper player status",
                "source_url": None,
            }
        else:
            player["injury_report"] = None
            player["availability"] = "No current designation"
            player["risk"] = 0.0
            continue

        lowered = status.lower()
        risk = next((value for key, value in STATUS_RISK.items() if key in lowered), 0.12)
        player["risk"] = risk
        player["availability"] = status
        # The recommendation model applies this risk after combining all positive signals.
        if risk >= 1:
            player["adjusted_projected"] = 0.0
        else:
            player["adjusted_projected"] = round(player["projected"] * (1 - risk), 1)
    return metrics

def apply_teammate_opportunity(metrics):
    """Estimate a backup's role expansion when higher-role position mates are hurt.

    Only injuries to teammates whose healthy role is materially larger than the
    candidate's count. This prevents a starter from being boosted because a fringe
    player at the same position is on IR.
    """
    from collections import defaultdict
    groups = defaultdict(list)
    for player in metrics.values():
        team = (player.get("team") or "").upper()
        position = player.get("position")
        if team and team != "FA" and position in CORE_POS:
            groups[(team, position)].append(player)

    for players in groups.values():
        max_projection = max((float(x.get("projected") or 0) for x in players), default=0) or 1
        max_recent = max((float(x.get("recent") or 0) for x in players), default=0) or 1
        max_usage = max((float(x.get("usage") or 0) for x in players), default=0) or 1

        def role(player):
            return (
                .45 * min(1, float(player.get("projected") or 0) / max_projection)
                + .35 * min(1, float(player.get("usage") or 0) / max_usage)
                + .20 * min(1, float(player.get("recent") or 0) / max_recent)
            )

        roles = {x["id"]: role(x) for x in players}
        for player in players:
            candidate_role = roles[player["id"]]
            unavailable = []
            remaining = 1.0
            for teammate in players:
                if teammate["id"] == player["id"]:
                    continue
                risk = float(teammate.get("risk") or 0)
                teammate_role = roles[teammate["id"]]
                # Count only a genuinely higher-depth-chart role—not injured fringe depth.
                if risk < 0.12 or teammate_role < 0.25 or teammate_role <= candidate_role * 1.05:
                    continue
                displaced = min(.95, risk * teammate_role)
                remaining *= 1 - displaced
                unavailable.append({
                    "name": teammate["name"],
                    "status": teammate.get("availability") or teammate.get("injury") or "Injured",
                    "role": round(teammate_role * 100),
                })

            displaced_role = 1 - remaining
            # Proven involvement makes a backup more likely to absorb the vacated role.
            readiness = (
                .30
                + .40 * min(1, float(player.get("usage") or 0) / max_usage)
                + .20 * min(1, float(player.get("projected") or 0) / max_projection)
                + .10 * min(1, float(player.get("recent") or 0) / max_recent)
            )
            healthy_share = max(0, 1 - float(player.get("risk") or 0))
            opportunity = round(min(100, 100 * displaced_role * readiness * healthy_share))
            player["teammate_opportunity"] = opportunity
            player["opportunity_bonus"] = round(opportunity * .10, 1)
            player["injured_position_mates"] = sorted(
                unavailable, key=lambda x: -x["role"]
            )[:4]

    for player in metrics.values():
        player.setdefault("teammate_opportunity", 0)
        player.setdefault("opportunity_bonus", 0.0)
        player.setdefault("injured_position_mates", [])
    return metrics

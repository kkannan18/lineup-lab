#!/usr/bin/env python3
import json, urllib.request, sys, pathlib
from datetime import datetime

SLEEPER = "https://api.sleeper.app"
OUT = pathlib.Path("api")

def fetch(url):
    print(f"  fetching {url.replace(SLEEPER, '')} ...", end=" ", flush=True)
    try:
        data = urllib.request.urlopen(url, timeout=30).read()
        print(f"{len(data)//1024}KB")
        return json.loads(data)
    except Exception as e:
        print(f"FAILED: {e}")
        return None

def save(name, data):
    path = OUT / name
    path.write_text(json.dumps(data, separators=(",", ":")))
    print(f"  saved {name} ({path.stat().st_size//1024}KB)")

PROJ_KEEP = {
    "pts_ppr","pts_std","pts_half_ppr",
    "pass_yd","pass_td","pass_att","pass_int",
    "rush_yd","rush_td","rush_att",
    "rec","rec_yd","rec_td","rec_tgt",
    "fgm","fgm_0_19","fgm_20_29","fgm_30_39","fgm_40_49","fgm_50p",
    "xpm","fga","xpa",
    "def_td","def_st_td","sack","int","fum_rec","safe","blk_kick","pts_allow",
}

print("Fetching NFL state...")
state = fetch(f"{SLEEPER}/v1/state/nfl")
if not state:
    sys.exit("Could not fetch NFL state")
season = state["season"]
week = int(state.get("week") or 1)
print(f"  season={season} week={week}")

print("Fetching players...")
raw_players = fetch(f"{SLEEPER}/v1/players/nfl")
if raw_players:
    POSITIONS = {"QB","RB","WR","TE","K","DEF"}
    KEEP = ("full_name","position","fantasy_positions","team","search_full_name")
    players = {
        pid: {k: p.get(k) for k in KEEP}
        for pid, p in raw_players.items()
        if p.get("team") and p.get("position") in POSITIONS
    }
    save("players.json", players)

print("Fetching projections...")
raw_proj = fetch(f"{SLEEPER}/projections/nfl/{season}/{week}?season_type=regular")
if raw_proj:
    # Handle both list and dict response formats
    if isinstance(raw_proj, list):
        raw_proj = {str(i): item for i, item in enumerate(raw_proj) if isinstance(item, dict)}
    # Projections are keyed by player_id inside each item
    proj = {}
    for pid, item in raw_proj.items():
        if not isinstance(item, dict):
            continue
        # Some formats nest stats under a key
        stats = item.get("stats") or item
        trimmed = {k: v for k, v in stats.items() if k in PROJ_KEEP}
        if trimmed:
            proj[pid] = trimmed
    save("projections.json", proj)

print("Fetching recent stats...")
for w in range(max(1, week - 2), week):
    raw_stats = fetch(f"{SLEEPER}/stats/nfl/{season}/{w}?season_type=regular")
    if raw_stats:
        if isinstance(raw_stats, list):
            raw_stats = {str(i): item for i, item in enumerate(raw_stats) if isinstance(item, dict)}
        stats_out = {}
        for pid, item in raw_stats.items():
            if not isinstance(item, dict):
                continue
            s = item.get("stats") or item
            trimmed = {k: v for k, v in s.items() if k in PROJ_KEEP}
            if trimmed:
                stats_out[pid] = trimmed
        save(f"stats_{season}_{w}.json", stats_out)

print(f"\nBuild complete — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")

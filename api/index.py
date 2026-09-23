import asyncio
import json
import time
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .portfolio_engine import (
    SLEEPER,
    build_portfolio,
    espn_team,
    make_feed,
)

ESPN = "https://lm-api-reads.fantasy.espn.com"
app = FastAPI(
    title="Lineup Lab Public API",
    docs_url=None,
    redoc_url=None,
)

# Ephemeral warm-instance cache only. Nothing is written to a database or disk.
_cache: dict[str, tuple[float, Any]] = {}
MAX_CACHE_ENTRIES = 96

# Pre-seed cache with bundled player snapshot to avoid cold-start download
try:
    import json as _json, pathlib as _pathlib, time as _time
    _snap = _pathlib.Path(__file__).parent / "players.json"
    if _snap.exists():
        _players = _json.loads(_snap.read_text())
        _SLEEPER = "https://api.sleeper.app"
        _cache[f"{_SLEEPER}/v1/players/nfl"] = (_time.time() + 82800, _players)
except Exception:
    pass


def cache_get(key: str):
    item = _cache.get(key)
    if item and item[0] > time.time():
        return item[1]
    _cache.pop(key, None)
    return None


def cache_put(key: str, value: Any, ttl: int):
    if len(_cache) >= MAX_CACHE_ENTRIES:
        oldest = min(_cache, key=lambda k: _cache[k][0])
        _cache.pop(oldest, None)
    _cache[key] = (time.time() + ttl, value)


async def get_json(url, *, cookies=None, ttl=900):
    cache_key = url if not cookies else None
    if cache_key:
        hit = cache_get(cache_key)
        if hit is not None:
            return hit
    async with httpx.AsyncClient(timeout=45, follow_redirects=True) as client:
        response = await client.get(url, cookies=cookies)
    if response.status_code == 404:
        return None
    if response.status_code in (401, 403):
        raise HTTPException(
            403,
            "That resource is private or unavailable. The public demo supports only public ESPN leagues.",
        )
    response.raise_for_status()
    value = response.json()
    if cache_key:
        cache_put(cache_key, value, ttl)
    return value


async def fetch_public_espn(league_id, season, secret, week=None):
    params = [
        ("view", "mRoster"),
        ("view", "mTeam"),
        ("view", "mSettings"),
        ("view", "mMatchup"),
    ]
    if week:
        params.append(("scoringPeriodId", str(week)))
    url = (
        f"{ESPN}/apis/v3/games/ffl/seasons/{season}"
        f"/segments/0/leagues/{league_id}"
    )
    async with httpx.AsyncClient(timeout=45, follow_redirects=True) as client:
        response = await client.get(url, params=params)
    if response.status_code in (401, 403):
        raise HTTPException(
            403,
            "This ESPN league is private. Use the secure PromptQL edition for private ESPN leagues.",
        )
    if response.status_code == 404:
        raise HTTPException(404, "ESPN league not found.")
    response.raise_for_status()
    return response.json()


class SleeperRequest(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    season: int | None = None
    week: int | None = None


class EspnLeagueRequest(BaseModel):
    league_id: str = Field(min_length=1, max_length=40)
    season: int | None = None


class EspnAnalyzeRequest(EspnLeagueRequest):
    team_id: str = Field(min_length=1, max_length=20)
    week: int | None = None


async def nfl_state():
    value = await get_json(f"{SLEEPER}/v1/state/nfl", ttl=300)
    if not value:
        raise HTTPException(503, "NFL week state is temporarily unavailable.")
    return value


def resolve_period(state, season, week):
    use_season = int(season or state.get("season"))
    use_week = int(week or state.get("week") or 1)
    return use_season, max(1, min(use_week, 18))


@app.exception_handler(httpx.HTTPError)
async def http_error_handler(_, error):
    return JSONResponse(
        status_code=502,
        content={"detail": f"A fantasy data provider is temporarily unavailable: {type(error).__name__}"},
    )


@app.get("/api/health")
async def health():
    return {"ok": True, "storage": "ephemeral-only"}


@app.get("/api/state")
async def state():
    current = await nfl_state()
    return {
        "nfl": current,
        "platforms": {
            "sleeper": "username",
            "espn": "public-league-only",
            "yahoo": "coming-soon-oauth",
        },
        "privacy": "No account, league, roster, or credential database is used.",
    }


@app.post("/api/analyze/sleeper")
async def analyze_sleeper(body: SleeperRequest):
    current = await nfl_state()
    season, week = resolve_period(current, body.season, body.week)
    username = body.username.strip()
    user = await get_json(f"{SLEEPER}/v1/user/{username}", ttl=120)
    if not user or not user.get("user_id"):
        raise HTTPException(404, "Sleeper username not found.")
    connection = {
        "platform": "sleeper",
        "account_key": str(user["user_id"]),
        "label": user.get("display_name") or username,
        "secret_json": "{}",
    }
    result = await build_portfolio(
        get_json, fetch_public_espn, [connection], season, week
    )
    result["viewer"] = {
        "platform": "Sleeper",
        "label": user.get("display_name") or username,
    }
    result["privacy"] = "Analyzed in memory; not saved to a database."
    return result


@app.post("/api/espn/teams")
async def espn_teams(body: EspnLeagueRequest):
    current = await nfl_state()
    season = int(body.season or current.get("season"))
    league = await fetch_public_espn(body.league_id.strip(), season, {}, None)
    settings = league.get("settings") or {}
    teams = [
        {
            "id": str(team.get("id")),
            "name": team.get("name")
            or " ".join(
                x for x in (team.get("location"), team.get("nickname")) if x
            )
            or f"Team {team.get('id')}",
        }
        for team in league.get("teams", [])
    ]
    return {
        "league": {
            "id": body.league_id.strip(),
            "name": settings.get("name") or f"ESPN League {body.league_id}",
            "season": season,
        },
        "teams": teams,
    }


@app.post("/api/analyze/espn")
async def analyze_espn(body: EspnAnalyzeRequest):
    current = await nfl_state()
    season, week = resolve_period(current, body.season, body.week)
    league = await fetch_public_espn(body.league_id.strip(), season, {}, week)
    label = (league.get("settings") or {}).get("name") or f"ESPN League {body.league_id}"
    feed = await make_feed(get_json, season, week)
    team, reason = await espn_team(
        fetch_public_espn,
        body.league_id.strip(),
        season,
        {"team_id": body.team_id},
        label,
        feed,
        week,
    )
    if not team:
        raise HTTPException(404, reason or "ESPN team unavailable.")
    return {
        "season": season,
        "week": week,
        "teams": [team],
        "skipped": [],
        "summary": {
            "leagues": 1,
            "lineup_changes": team.get("changes", 0),
            "waiver_targets": len(team.get("waivers", [])),
            "trade_targets": len(team.get("trade_targets", [])),
            "injury_alerts": team.get("urgent_alerts", 0),
        },
        "method": "League-aware projection, form, involvement, matchup, consistency, and injury-opportunity model",
        "viewer": {"platform": "ESPN", "label": team.get("team")},
        "privacy": "Analyzed in memory; not saved to a database.",
    }

#!/usr/bin/env python3
"""
mfl_fetch.py — pull a MyFantasyLeague team's state into JSON snapshots.

Designed to run unattended in GitHub Actions on a schedule, committing the
results so an assistant can read them. Uses only the Python standard library.

Config comes from environment variables:

  MFL_LEAGUE_ID   (required)  your league's numeric ID, the L= in the URL
  MFL_YEAR        (default: current season)
  MFL_API_KEY     (optional)  needed for private-league data; keep in secrets
  MFL_FRANCHISE   (optional)  your franchise ID, e.g. "0003"
  MFL_HOST        (default: api.myfantasyleague.com)
  MFL_WEEK        (optional)  force a week instead of auto-detecting
  MFL_OUT         (default: data)
  MFL_CONTACT     (optional)  contact string for the User-Agent

MFL asks API clients to identify themselves and to be gentle with request
volume, so every call carries a descriptive User-Agent and calls are spaced
out. The big players file is only refreshed weekly.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

HOST = os.environ.get("MFL_HOST", "api.myfantasyleague.com").strip()
LEAGUE_ID = os.environ.get("MFL_LEAGUE_ID", "").strip()
API_KEY = os.environ.get("MFL_API_KEY", "").strip()
FRANCHISE = os.environ.get("MFL_FRANCHISE", "").strip()
OUT_DIR = Path(os.environ.get("MFL_OUT", "data"))
CONTACT = os.environ.get("MFL_CONTACT", "").strip()

_now = datetime.now(timezone.utc)
# The MFL "year" rolls to the new season in the spring, but for our purposes
# anything before March still belongs to the previous season.
DEFAULT_YEAR = _now.year if _now.month >= 3 else _now.year - 1
YEAR = os.environ.get("MFL_YEAR", str(DEFAULT_YEAR)).strip()

USER_AGENT = "mfl-advisor/1.0 (personal league assistant{})".format(
    f"; {CONTACT}" if CONTACT else ""
)

REQUEST_SPACING_SEC = 1.5
PLAYERS_CACHE_MAX_AGE_HOURS = 24 * 6

# Fatal, because everything downstream is keyed on the league.
if not LEAGUE_ID:
    sys.exit("MFL_LEAGUE_ID is not set. See README.md for where to find it.")


# --------------------------------------------------------------------------
# http
# --------------------------------------------------------------------------

_last_request_at = 0.0


def mfl_get(req_type: str, **params) -> dict | list | None:
    """Call one MFL export endpoint and return parsed JSON, or None on failure.

    A single failed endpoint should never kill the whole run — a snapshot
    missing 'tradeBait' is still worth having.
    """
    global _last_request_at

    query = {"TYPE": req_type, "L": LEAGUE_ID, "JSON": "1"}
    for key, value in params.items():
        if value not in (None, ""):
            query[key] = str(value)
    if API_KEY:
        query["APIKEY"] = API_KEY

    url = f"https://{HOST}/{YEAR}/export?" + urllib.parse.urlencode(query)

    # Be a polite client: space out requests regardless of how fast we loop.
    elapsed = time.monotonic() - _last_request_at
    if elapsed < REQUEST_SPACING_SEC:
        time.sleep(REQUEST_SPACING_SEC - elapsed)

    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        print(f"  ! {req_type}: HTTP {exc.code}", file=sys.stderr)
        return None
    except Exception as exc:  # noqa: BLE001 - network failure of any kind
        print(f"  ! {req_type}: {exc}", file=sys.stderr)
        return None
    finally:
        _last_request_at = time.monotonic()

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        print(f"  ! {req_type}: response was not JSON", file=sys.stderr)
        return None

    # MFL reports its own errors inside a 200 response.
    if isinstance(payload, dict) and "error" in payload:
        message = payload["error"]
        if isinstance(message, dict):
            message = message.get("$t", message)
        print(f"  ! {req_type}: MFL error: {message}", file=sys.stderr)
        return None

    return payload


def unwrap(payload, *keys):
    """Walk MFL's nested single-key wrappers, tolerating missing levels."""
    node = payload
    for key in keys:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def as_list(node) -> list:
    """MFL returns a bare object instead of a list when there's exactly one."""
    if node is None:
        return []
    return node if isinstance(node, list) else [node]


# --------------------------------------------------------------------------
# week detection
# --------------------------------------------------------------------------


def detect_week() -> int:
    """Figure out the week in play from the NFL schedule's kickoff times.

    Preferred over a hardcoded season-start date, which silently rots. The
    'current' week is the earliest week that still has a game more than three
    hours in the future; once every game has kicked off we've moved on.
    """
    forced = os.environ.get("MFL_WEEK", "").strip()
    if forced:
        return int(forced)

    schedule = mfl_get("nflSchedule", W="ALL")
    weeks = as_list(unwrap(schedule, "nflSchedule"))

    now_ts = _now.timestamp()
    best = None

    for week_node in weeks:
        try:
            number = int(week_node.get("week"))
        except (TypeError, ValueError):
            continue

        kickoffs = []
        for game in as_list(week_node.get("matchup")):
            try:
                kickoffs.append(int(game.get("kickoff")))
            except (TypeError, ValueError):
                continue
        if not kickoffs:
            continue

        # Still live if any game hasn't started, or the last one ended recently.
        if max(kickoffs) + (3 * 3600) > now_ts:
            if best is None or number < best:
                best = number

    if best is not None:
        return max(1, min(18, best))

    print("  ! could not detect week from schedule; defaulting to 1", file=sys.stderr)
    return 1


# --------------------------------------------------------------------------
# fetch plan
# --------------------------------------------------------------------------


def players_cache_is_fresh(path: Path) -> bool:
    if not path.exists():
        return False
    age = datetime.now(timezone.utc) - datetime.fromtimestamp(
        path.stat().st_mtime, timezone.utc
    )
    return age < timedelta(hours=PLAYERS_CACHE_MAX_AGE_HOURS)


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    week = detect_week()
    print(f"MFL league {LEAGUE_ID}, {YEAR} season, week {week}")

    # (filename, TYPE, params) — order roughly by how much we care.
    plan = [
        ("league", "league", {}),
        ("rosters", "rosters", {}),
        ("standings", "leagueStandings", {}),
        ("schedule", "schedule", {"W": week}),
        ("live_scoring", "liveScoring", {"W": week}),
        ("projected_scores", "projectedScores", {"W": week}),
        ("player_scores", "playerScores", {"W": "YTD"}),
        ("free_agents", "freeAgents", {}),
        ("injuries", "injuries", {"W": week}),
        ("transactions", "transactions", {}),
        ("trade_bait", "tradeBait", {}),
        ("pending_trades", "pendingTrades", {}),
        ("adp", "adp", {}),
        ("nfl_schedule", "nflSchedule", {"W": week}),
    ]

    written = {}
    failed = []

    for name, req_type, params in plan:
        print(f"  - {req_type}")
        payload = mfl_get(req_type, **params)
        if payload is None:
            failed.append(req_type)
            continue
        path = OUT_DIR / f"{name}.json"
        path.write_text(json.dumps(payload, indent=1, sort_keys=True))
        written[name] = str(path)

    # The player dictionary is several MB and changes slowly. Refresh weekly.
    players_path = OUT_DIR / "players.json"
    if players_cache_is_fresh(players_path):
        print("  - players (cached)")
        written["players"] = str(players_path)
    else:
        print("  - players")
        payload = mfl_get("players", DETAILS="1")
        if payload is None:
            failed.append("players")
        else:
            players_path.write_text(json.dumps(payload, indent=1, sort_keys=True))
            written["players"] = str(players_path)

    # A small index so a reader knows what it's looking at without opening
    # every file.
    manifest = {
        "fetched_at": _now.isoformat(),
        "season": YEAR,
        "week": week,
        "league_id": LEAGUE_ID,
        "franchise_id": FRANCHISE or None,
        "files": written,
        "failed": failed,
        "authenticated": bool(API_KEY),
    }
    (OUT_DIR / "manifest.json").write_text(json.dumps(manifest, indent=1))

    if failed:
        print(f"\nDone, with {len(failed)} endpoint(s) unavailable: {', '.join(failed)}")
    else:
        print(f"\nDone. {len(written)} files written to {OUT_DIR}/")

    # Exit 0 even on partial failure — a partial snapshot still gets committed.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

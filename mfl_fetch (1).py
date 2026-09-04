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


# --------------------------------------------------------------------------
# summary — resolve IDs to names so downstream readers never need players.json
# --------------------------------------------------------------------------

SKILL_POSITIONS = ("QB", "RB", "WR", "TE", "PK", "K", "DEF", "Def")
FREE_AGENT_CAP = 400
TRANSACTION_CAP = 40


def load_json(name: str):
    path = OUT_DIR / f"{name}.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def tidy_name(raw: str) -> str:
    """MFL stores names as 'Last, First'. Humans read 'First Last'."""
    if not raw:
        return ""
    if ", " in raw:
        last, first = raw.split(", ", 1)
        return f"{first} {last}"
    return raw


def build_player_index() -> dict:
    index = {}
    for player in as_list(unwrap(load_json("players"), "players", "player")):
        pid = player.get("id")
        if not pid:
            continue
        index[pid] = {
            "name": tidy_name(player.get("name", "")),
            "pos": player.get("position", ""),
            "team": player.get("team", ""),
        }
    return index


def score_index(name: str) -> dict:
    """id -> float, from a playerScores/projectedScores payload."""
    node = load_json(name)
    key = "playerScores" if "playerScores" in node else "projectedScores"
    out = {}
    for entry in as_list(unwrap(node, key, "playerScore")):
        try:
            out[entry.get("id")] = float(entry.get("score", 0) or 0)
        except (TypeError, ValueError):
            continue
    return out


def describe(pid: str, players: dict, ytd: dict, proj: dict, injuries: dict) -> dict:
    info = players.get(pid, {})
    row = {
        "id": pid,
        "name": info.get("name") or f"unknown player {pid}",
        "pos": info.get("pos", ""),
        "team": info.get("team", ""),
    }
    if pid in ytd:
        row["ytd"] = round(ytd[pid], 1)
    if pid in proj:
        row["proj"] = round(proj[pid], 1)
    if pid in injuries:
        row["injury"] = injuries[pid]
    return row


def build_summary(week: int) -> dict:
    players = build_player_index()
    ytd = score_index("player_scores")
    proj = score_index("projected_scores")

    injuries = {}
    for entry in as_list(unwrap(load_json("injuries"), "injuries", "injury")):
        pid = entry.get("id")
        if pid:
            status = entry.get("status", "")
            details = entry.get("details", "")
            injuries[pid] = f"{status} — {details}".strip(" —") or status

    league = unwrap(load_json("league"), "league") or {}

    franchise_names = {}
    for fr in as_list(unwrap(league, "franchises", "franchise")):
        if fr.get("id"):
            franchise_names[fr["id"]] = fr.get("name", fr["id"])

    record = {}
    for fr in as_list(unwrap(load_json("standings"), "leagueStandings", "franchise")):
        fid = fr.get("id")
        if not fid:
            continue
        record[fid] = {
            "w": fr.get("h2hw", "0"),
            "l": fr.get("h2hl", "0"),
            "t": fr.get("h2ht", "0"),
            "pf": fr.get("pf", "0"),
        }

    teams = {}
    mine = []
    for fr in as_list(unwrap(load_json("rosters"), "rosters", "franchise")):
        fid = fr.get("id")
        if not fid:
            continue
        squad = []
        for slot in as_list(fr.get("player")):
            pid = slot.get("id")
            if not pid:
                continue
            row = describe(pid, players, ytd, proj, injuries)
            status = slot.get("status", "")
            if status and status != "ROSTER":
                row["slot"] = status
            if slot.get("salary"):
                row["salary"] = slot["salary"]
            squad.append(row)
        squad.sort(key=lambda r: (r.get("pos", ""), -r.get("ytd", 0)))
        teams[fid] = {
            "name": franchise_names.get(fid, fid),
            "record": record.get(fid, {}),
            "players": squad,
        }
        if fid == FRANCHISE:
            mine = squad

    agents = []
    fa_node = unwrap(load_json("free_agents"), "freeAgents", "leagueUnit")
    for unit in as_list(fa_node):
        for slot in as_list(unit.get("player")):
            pid = slot.get("id")
            if not pid:
                continue
            row = describe(pid, players, ytd, proj, injuries)
            if row["pos"] in SKILL_POSITIONS:
                agents.append(row)
    agents.sort(key=lambda r: (-r.get("proj", 0), -r.get("ytd", 0), r["name"]))
    agents = agents[:FREE_AGENT_CAP]

    moves = []
    for entry in as_list(unwrap(load_json("transactions"), "transactions", "transaction")):
        moves.append({
            "type": entry.get("type", ""),
            "franchise": franchise_names.get(entry.get("franchise"), entry.get("franchise")),
            "when": entry.get("timestamp", ""),
            "detail": entry.get("transaction", ""),
        })
    moves = moves[:TRANSACTION_CAP]

    return {
        "generated_at": _now.isoformat(),
        "season": YEAR,
        "week": week,
        "league": {
            "name": league.get("name", ""),
            "id": LEAGUE_ID,
            "roster_size": league.get("rosterSize", ""),
            "franchise_count": len(teams),
        },
        "me": {
            "franchise_id": FRANCHISE,
            "name": franchise_names.get(FRANCHISE, ""),
            "record": record.get(FRANCHISE, {}),
            "roster": mine,
        },
        "teams": teams,
        "free_agents": agents,
        "recent_transactions": moves,
    }


def render_markdown(summary: dict) -> str:
    """A version you can read on your phone."""
    lines = []
    league = summary["league"]
    me = summary["me"]

    lines.append(f"# {me.get('name') or 'My team'} — week {summary['week']}")
    lines.append("")
    lines.append(f"{league.get('name', '')} · {league.get('franchise_count', 0)} teams · "
                 f"generated {summary['generated_at'][:16].replace('T', ' ')} UTC")
    rec = me.get("record") or {}
    if rec:
        lines.append(f"Record {rec.get('w', 0)}-{rec.get('l', 0)}-{rec.get('t', 0)}, "
                     f"{rec.get('pf', 0)} points for")
    lines.append("")

    def table(rows, heading):
        if not rows:
            return
        lines.append(f"## {heading}")
        lines.append("")
        lines.append("| Player | Pos | Team | Proj | YTD | Note |")
        lines.append("|---|---|---|---|---|---|")
        for r in rows:
            note = r.get("injury", "") or r.get("slot", "")
            lines.append(
                f"| {r['name']} | {r.get('pos', '')} | {r.get('team', '')} | "
                f"{r.get('proj', '')} | {r.get('ytd', '')} | {note} |"
            )
        lines.append("")

    table(me.get("roster", []), "Roster")

    by_pos = {}
    for row in summary.get("free_agents", []):
        by_pos.setdefault(row.get("pos", "?"), []).append(row)
    if by_pos:
        lines.append("## Best available")
        lines.append("")
        for pos in ("QB", "RB", "WR", "TE", "PK", "K", "DEF", "Def"):
            if pos in by_pos:
                top = by_pos[pos][:8]
                names = ", ".join(
                    f"{r['name']} ({r.get('team', '')})" for r in top
                )
                lines.append(f"**{pos}** — {names}")
                lines.append("")

    hurt = [r for r in me.get("roster", []) if r.get("injury")]
    if hurt:
        lines.append("## Injuries on your roster")
        lines.append("")
        for r in hurt:
            lines.append(f"- **{r['name']}** ({r.get('pos', '')}) — {r['injury']}")
        lines.append("")

    return "\n".join(lines)


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

    # Join everything against the player dictionary once, here, so that
    # anything reading this repo later gets names instead of ID numbers and
    # never has to open the multi-megabyte players file.
    summary_written = False
    try:
        summary = build_summary(week)
        (OUT_DIR / "summary.json").write_text(json.dumps(summary, indent=1))
        (OUT_DIR / "summary.md").write_text(render_markdown(summary))
        summary_written = True
        roster_size = len(summary["me"]["roster"])
        print(f"\nSummary: {roster_size} players on {summary['me']['name'] or 'your roster'}, "
              f"{len(summary['free_agents'])} free agents listed")
        if not roster_size:
            print("  ! your franchise ID matched no roster — check MFL_FRANCHISE",
                  file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 - never lose the raw snapshot
        print(f"  ! summary step failed: {exc}", file=sys.stderr)

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
        "summary": "data/summary.json" if summary_written else None,
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

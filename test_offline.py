"""Offline check of mfl_fetch.py against mocked MFL responses.

No network. Confirms week detection, wrapper unwrapping, error tolerance,
and the shape of what lands on disk.
"""

import io
import json
import os
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

TMP = tempfile.mkdtemp()
os.environ.update(
    MFL_LEAGUE_ID="12345",
    MFL_YEAR="2026",
    MFL_FRANCHISE="0003",
    MFL_OUT=TMP,
)
os.environ.pop("MFL_WEEK", None)
os.environ.pop("MFL_API_KEY", None)

NOW = datetime.now(timezone.utc).timestamp()

# Weeks 1 and 2 already kicked off and finished; week 3 is upcoming.
SCHEDULE = {
    "nflSchedule": [
        {"week": "1", "matchup": [{"kickoff": str(int(NOW - 86400 * 14))}]},
        {"week": "2", "matchup": [{"kickoff": str(int(NOW - 86400 * 7))}]},
        {"week": "3", "matchup": [
            {"kickoff": str(int(NOW + 86400 * 2))},
            {"kickoff": str(int(NOW + 86400 * 4))},
        ]},
        {"week": "4", "matchup": {"kickoff": str(int(NOW + 86400 * 9))}},  # single-game
    ]
}

CALLS = []


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def fake_urlopen(request, timeout=None):
    url = request.full_url
    assert request.get_header("User-agent", "").startswith("mfl-advisor/"), \
        "requests must identify the client"
    params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    req_type = params["TYPE"][0]
    CALLS.append(req_type)

    assert params["L"] == ["12345"], "league id must be on every call"
    assert params["JSON"] == ["1"], "JSON flag must be on every call"

    if req_type == "nflSchedule" and params.get("W") == ["ALL"]:
        body = SCHEDULE
    elif req_type == "tradeBait":
        # Exercise the MFL-error-inside-a-200 path.
        body = {"error": {"$t": "No trade bait posted"}}
    elif req_type == "pendingTrades":
        # Exercise the hard-failure path.
        raise urllib.error.HTTPError(url, 503, "Service Unavailable", {}, None)
    elif req_type == "adp":
        # Exercise the non-JSON path.
        return FakeResponse(b"<html>maintenance</html>")
    elif req_type == "rosters":
        body = {"rosters": {"franchise": [
            {"id": "0003", "player": [{"id": "13593", "status": "ROSTER"}]}
        ]}}
    elif req_type == "players":
        body = {"players": {"player": [{"id": "13593", "name": "Test, Player"}]}}
    else:
        body = {req_type: {"ok": True, "week": params.get("W", [None])[0]}}

    return FakeResponse(json.dumps(body).encode())


urllib.request.urlopen = fake_urlopen

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mfl_fetch  # noqa: E402

mfl_fetch.REQUEST_SPACING_SEC = 0  # don't actually sleep through the test

rc = mfl_fetch.main()

# ---- assertions ----------------------------------------------------------

failures = []


def check(condition, label):
    if condition:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}")
        failures.append(label)


print("\nResults:")
check(rc == 0, "exits 0 even with three broken endpoints")

manifest = json.loads(open(os.path.join(TMP, "manifest.json")).read())
check(manifest["week"] == 3, f"detected week 3 (got {manifest['week']})")
check(manifest["league_id"] == "12345", "league id recorded")
check(manifest["franchise_id"] == "0003", "franchise id recorded")
check(manifest["authenticated"] is False, "flags that no API key was used")

check(sorted(manifest["failed"]) == ["adp", "pendingTrades", "tradeBait"],
      f"all three failure modes caught (got {sorted(manifest['failed'])})")

for name in ("league", "rosters", "free_agents", "projected_scores", "players"):
    check(name in manifest["files"], f"wrote {name}.json")

check(not os.path.exists(os.path.join(TMP, "tradeBait.json")),
      "no file written for a failed endpoint")

rosters = json.loads(open(os.path.join(TMP, "rosters.json")).read())
check(mfl_fetch.unwrap(rosters, "rosters", "franchise") is not None,
      "unwrap() walks nested wrappers")
check(mfl_fetch.unwrap(rosters, "rosters", "nope") is None,
      "unwrap() tolerates a missing key")
check(len(mfl_fetch.as_list({"a": 1})) == 1 and len(mfl_fetch.as_list(None)) == 0,
      "as_list() normalises single objects and None")

# Week-scoped endpoints should have asked for the detected week.
proj = json.loads(open(os.path.join(TMP, "projected_scores.json")).read())
check(proj["projectedScores"]["week"] == "3", "week-scoped calls used week 3")

check(CALLS.count("players") == 1, "players fetched once")

# Second run should reuse the cached players file.
CALLS.clear()
mfl_fetch.main()
check(CALLS.count("players") == 0, "players served from cache on rerun")

print()
if failures:
    print(f"{len(failures)} check(s) failed.")
    raise SystemExit(1)
print("All checks passed.")

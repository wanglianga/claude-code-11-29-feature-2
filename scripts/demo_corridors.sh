#!/usr/bin/env bash
# Corridor (活动通道候选集) demonstration and self-check against a running service.
#
#   ./scripts/demo_corridors.sh                 # uses http://127.0.0.1:8000
#   BASE=http://host:8000 ./scripts/demo_corridors.sh
#
# For every corridor scenario it resets the service, loads the simulated
# uploads, runs the event pipeline, creates the scenario's constraint books,
# executes the configured corridor runs (both solver methods where specified)
# and finally runs the independent corridor verifier.
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8000}"
KEY="${WETLAND_API_KEY:-dev-api-key}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${PYTHON:-$ROOT/.venv/bin/python}"
[ -x "$PY" ] || PY=python3

cd "$ROOT"

echo "service: $BASE"
"$PY" - <<PY
import json, urllib.request
with urllib.request.urlopen("$BASE/health", timeout=5) as r:
    print("health:", json.load(r))
PY

run_scenario() {
  local scenario="$1"
  echo
  echo "=== $scenario ==="
  "$PY" -m simulator.run_simulator --scenario "$scenario" --base-url "$BASE" --reset > /dev/null
  "$PY" - <<PY
import json, urllib.request
req = urllib.request.Request("$BASE/runs", data=b'{"wait": true}',
                             headers={"Content-Type": "application/json", "X-API-Key": "$KEY"})
print("pipeline:", json.load(urllib.request.urlopen(req))["status"])
PY
  "$PY" -m simulator.corridor_driver --scenario "$scenario" --base-url "$BASE" --api-key "$KEY"
  "$PY" -m verifier.verify_corridors --scenario "$scenario" --base-url "$BASE" --api-key "$KEY"
}

# 1. Crossing routes: near-tie swaps at the crossing stay published as
#    competing candidates; noise events stay unattributed.
run_scenario crossing

# 2. Near-simultaneous calls: never chained into one corridor; mirror ties
#    surface as tied competitors.
run_scenario simultaneous

# 3. Long blind zone: the corridor spans the outage; the gap cites the blind
#    zone and carries no coordinates.
run_scenario blindgap

# 4. Clock-policy versions: the tight edge exists only under the loose
#    cross-station clock uncertainty; the two runs pin different book hashes.
run_scenario clockskew

echo
echo "All corridor scenarios verified."

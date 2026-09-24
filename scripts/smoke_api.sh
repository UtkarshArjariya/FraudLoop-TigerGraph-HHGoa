#!/usr/bin/env bash
set -euo pipefail
base="${FRAUDLOOP_API_URL:-http://127.0.0.1:8000}"
python_bin="${FRAUDLOOP_PYTHON:-.venv/bin/python}"
scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT
request() {
  local path="$1"
  curl --fail-with-body --silent --show-error --max-time 180 "$base$path" "${@:2}"
}
request /api/health > "$scratch/health.json"
request /api/cases > "$scratch/cases.json"
case_id="$($python_bin -c 'import json,sys; rows=json.load(open(sys.argv[1]))["cases"]; print(next(row["case_id"] for row in rows if row["case_id"].startswith("HHG-")))' "$scratch/cases.json")"
request "/api/cases/$case_id" > "$scratch/case.json"
request "/api/cases/$case_id/timeline" > "$scratch/timeline.json"
request "/api/cases/$case_id/graph" > "$scratch/graph.json"
request "/api/cases/$case_id/precedents" > "$scratch/precedents.json"
request /api/approvals > "$scratch/approvals.json"
request /api/memory/stats > "$scratch/memory.json"
request "/api/cases/$case_id/investigate?mode=replay" -X POST > "$scratch/replay.sse"
txn_id="$($python_bin -c 'import json,sys; print(json.load(open(sys.argv[1]))["trigger"]["flagged_txn_id"])' "$scratch/case.json")"
request /api/investigations -H 'Content-Type: application/json' -X POST \
  --data "{\"type\":\"risk_signal\",\"txn_id\":\"$txn_id\"}" > "$scratch/new.json"
approval_id="$($python_bin -c 'import json,sys; rows=json.load(open(sys.argv[1]))["pending"]; print(rows[0]["id"] if rows else "no-pending")' "$scratch/approvals.json")"
code="$(curl --silent --show-error --max-time 30 -o "$scratch/forbidden.json" -w '%{http_code}' \
  "$base/api/approvals/$approval_id" -X POST -H 'Content-Type: application/json' -H 'X-Role: viewer' \
  --data '{"decision":"approve","note":"Expected forbidden smoke check"}')"
test "$code" = 403
"$python_bin" - "$scratch" <<'PY'
import json, pathlib, sys
folder = pathlib.Path(sys.argv[1])
for file in folder.glob('*.json'):
    json.loads(file.read_text())
assert 'event: done' in (folder / 'replay.sse').read_text(), 'Replay did not complete'
assert len(json.loads((folder / 'graph.json').read_text())['nodes']) <= 150
print('API smoke passed: all endpoints returned success; viewer approval correctly returned 403.')
print('A queued runtime trigger was created. No live investigation, bank action, model call, or approval was executed.')
PY

#!/usr/bin/env bash
# Explicit graded-answer regeneration. Never attempts provisioning or embeddings.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
python_bin="${FRAUDLOOP_PYTHON:-.venv/bin/python}"

check_gse() {
  local state
  state="$(docker exec -u tigergraph tg bash -ic 'gadmin status gse' 2>/dev/null)" || return 1
  # Check negative states before accepting the service output.
  if [[ "$state" =~ [Dd]own|[Ss]topped|[Oo]ffline ]]; then
    return 2
  fi
  [[ "$state" =~ [Rr]unning|[Oo]nline ]]
}

status=0
check_gse || status=$?
if [[ "$status" -eq 2 ]]; then
  printf 'GSE is down; attempting one authorized service restart.\n' >&2
  docker exec -u tigergraph tg bash -ic 'gadmin restart gse -y'
  sleep 30
  status=0
  check_gse || status=$?
fi
if [[ "$status" -ne 0 ]]; then
  printf 'GSE is unavailable or its status is unknown. Skipping graded-answer and graph writes; use FRAUDLOOP_LOCAL_ONLY=1 make demo for saved/local views.\n' >&2
  exit 1
fi

exec "$python_bin" -m scripts.run_cases "$@"

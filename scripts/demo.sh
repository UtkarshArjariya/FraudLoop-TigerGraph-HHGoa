#!/usr/bin/env bash
# Start the local console and stop only these child process groups on exit.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

python_bin="${FRAUDLOOP_PYTHON:-.venv/bin/python}"
npm_bin="${FRAUDLOOP_NPM:-npm}"
backend_pid=""
frontend_pid=""

cleanup() {
  trap - EXIT INT TERM
  for pid in "$frontend_pid" "$backend_pid"; do
    if [[ -n "$pid" ]]; then
      kill -TERM -- "-$pid" 2>/dev/null || true
    fi
  done
  # A blocked live request must not leave a server behind after Ctrl+C.
  for attempt in {1..20}; do
    running=0
    for pid in "$frontend_pid" "$backend_pid"; do
      if [[ -n "$pid" ]] && kill -0 -- "-$pid" 2>/dev/null; then
        running=1
      fi
    done
    [[ "$running" -eq 0 ]] && break
    sleep 0.25
  done
  for pid in "$frontend_pid" "$backend_pid"; do
    if [[ -n "$pid" ]]; then
      kill -KILL -- "-$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
    fi
  done
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [[ ! -x "$python_bin" ]]; then
  printf 'Python environment missing: %s. Follow README setup first.\n' "$python_bin" >&2
  exit 1
fi
command -v "$npm_bin" >/dev/null
command -v setsid >/dev/null
if [[ ! -d frontend/node_modules ]]; then
  printf 'Frontend dependencies missing. Run npm --prefix frontend ci first.\n' >&2
  exit 1
fi
"$python_bin" - <<'PY'
import socket
import sys
for port in (8000, 5173):
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            sys.exit(f"Port {port} is already in use; stop that server before make demo.")
PY

# No reload/workers fan-out: live runs already use a serialized MCP lock.
setsid "$python_bin" -m uvicorn backend.app:app --host 127.0.0.1 --port 8000 --workers 1 &
backend_pid=$!
VITE_API_PROXY=http://127.0.0.1:8000 setsid "$npm_bin" --prefix frontend run dev &
frontend_pid=$!

printf '\nFRAUDLOOP: http://localhost:5173\nAPI: http://127.0.0.1:8000\nAPI docs: http://127.0.0.1:8000/docs\nCtrl+C stops both servers. Use Replay for the saved demo.\n\n'
# If either server exits, terminate its peer instead of leaving an orphan.
set +e
wait -n "$backend_pid" "$frontend_pid"
status=$?
set -e
exit "$status"

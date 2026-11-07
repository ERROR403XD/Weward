#!/usr/bin/env bash
set -euo pipefail

bash /app/prepare-storage.sh
/webox/weagent/bin/entrypoint.sh &
webox_pid=$!
python3 -m weward.collector &
collector_pid=$!

stop_children() {
  trap - TERM INT EXIT
  kill "$collector_pid" "$webox_pid" 2>/dev/null || true
  wait "$collector_pid" "$webox_pid" 2>/dev/null || true
}

trap 'stop_children; exit 143' TERM INT
trap stop_children EXIT
set +e
wait -n "$webox_pid" "$collector_pid"
status=$?
set -e
if [ "$status" -eq 0 ]; then status=1; fi
exit "$status"

#!/bin/sh
set -e

python -u api.py &
API_PID=$!

python -u main.py &
MAIN_PID=$!

PIDS="$API_PID $MAIN_PID"
if [ -f instagram.py ]; then
  python -u instagram.py &
  INSTAGRAM_PID=$!
  PIDS="$PIDS $INSTAGRAM_PID"
fi

trap 'kill $PIDS 2>/dev/null; wait' TERM INT

# shellcheck disable=SC2086
wait $PIDS

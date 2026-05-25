#!/bin/sh
set -e

python -u api.py &
API_PID=$!

python -u main.py &
MAIN_PID=$!

python -u instagram.py &
INSTAGRAM_PID=$!

trap 'kill "$API_PID" "$MAIN_PID" "$INSTAGRAM_PID" 2>/dev/null; wait' TERM INT

wait "$API_PID" "$MAIN_PID" "$INSTAGRAM_PID"

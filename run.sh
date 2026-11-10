#!/usr/bin/env bash
# Simple 1-command runner for agy2api
cd "$(dirname "$0")"

exec python3 web.py "$@"

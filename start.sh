#!/bin/bash
PORT="${SERVER_PORT:-${PORT:-9785}}"
HOST="${HOST:-0.0.0.0}"
exec python -m uvicorn server.main:app --host "$HOST" --port "$PORT"

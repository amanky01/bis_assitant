#!/usr/bin/env bash
# Run the backend with the venv's Python so the reloader subprocess sees venv packages.
# Do NOT run "python app/main.py" — the app package must be imported from the project root.
set -e
cd "$(dirname "$0")"

# Free port 8000 if something is already using it
if command -v lsof >/dev/null 2>&1; then
  pid=$(lsof -ti:8000 2>/dev/null || true)
  if [ -n "$pid" ]; then
    echo "Killing process on port 8000 (PID $pid)..."
    kill -9 $pid 2>/dev/null || true
    sleep 1
  fi
fi

exec .venv/bin/python -m uvicorn app.main:app --reload

#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
exec uv run --locked uvicorn app.main:app --reload "$@"

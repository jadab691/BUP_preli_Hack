#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
export LLM_BASE_URL="${LLM_BASE_URL:-https://api.openai.com/v1}"
export LLM_MODEL="${LLM_MODEL:-gpt-4o-mini}"

exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
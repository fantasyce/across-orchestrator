#!/usr/bin/env bash
set -euo pipefail

if [ -d "node_modules" ]; then
  export NODE_PATH="${PWD}/node_modules${NODE_PATH:+:${NODE_PATH}}"
fi
export PYTHONDONTWRITEBYTECODE=1
PYTHON_BIN="${PYTHON_BIN:-python3}"
if [[ -x ".venv/bin/python" ]]; then
  PYTHON_BIN=".venv/bin/python"
fi

echo "== whitespace =="
git diff --check

echo "== tests =="
PYTHONPATH=src "$PYTHON_BIN" -m pytest -p no:cacheprovider tests -q

echo "== cli smoke =="
PYTHONPATH=src "$PYTHON_BIN" -m across_orchestrator.cli --help >/dev/null
PYTHONPATH=src "$PYTHON_BIN" -m across_orchestrator.cli agent-card --json >/dev/null
PYTHONPATH=src "$PYTHON_BIN" -m across_orchestrator.cli mcp </dev/null >/dev/null

echo "== sensitive text scan =="
PATH_PATTERN='/U''sers/[^[:space:])]+'
TOKEN_PATTERN='(^|[^A-Za-z0-9_])(gho_''[A-Za-z0-9_]{20,}|sk-''[A-Za-z0-9_-]{20,})'
SENSITIVE_PATTERN="(${PATH_PATTERN}|${TOKEN_PATTERN})"
if command -v rg >/dev/null 2>&1; then
  if rg -n --hidden -g '!.git/**' -g '!*.pyc' "$SENSITIVE_PATTERN" .; then
    echo "Potential secret, private path, or signing metadata found." >&2
    exit 1
  fi
else
  if git grep -n -E "$SENSITIVE_PATTERN" -- .; then
    echo "Potential secret, private path, or signing metadata found." >&2
    exit 1
  fi
fi

echo "Across Orchestrator checks passed."

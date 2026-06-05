#!/bin/bash
# SessionStart hook for Claude Code on the web.
# Installs Python dependencies (incl. dev tools) so tests, ruff, and mypy work.
set -euo pipefail

# Only run in the remote (Claude Code on the web) environment.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-.}"

# Project imports modules from the repo root (e.g. `from analysis.match_state import ...`),
# so make sure the root is on PYTHONPATH for any direct script runs.
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  echo 'export PYTHONPATH="."' >> "$CLAUDE_ENV_FILE"
fi

# Install core + dev dependencies (pytest, pytest-asyncio, ruff, mypy).
# Plain `pip install` (not a locked sync) so the cached container keeps the env warm.
python -m pip install -r requirements-dev.txt

echo "session-start: dependencies installed"

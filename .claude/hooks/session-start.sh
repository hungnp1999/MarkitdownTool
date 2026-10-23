#!/usr/bin/env bash
# Enable the repo git hooks; install dependencies in Claude Code cloud sessions only.
set -euo pipefail
cd "${CLAUDE_PROJECT_DIR:-.}"
# Add the Dev-Environment trailer to commits (docs/DEVELOPMENT_PROVENANCE.md).
git config core.hooksPath .githooks
[ "${CLAUDE_CODE_REMOTE:-}" = "true" ] || exit 0
uv venv --allow-existing --python 3.12 .venv
uv pip install --python .venv --prerelease=allow -r requirements.txt

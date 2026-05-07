#!/bin/sh
# scripts/lock-deps.sh — regenerate requirements.lock.
#
# Why this exists (P5-03 in docs/security-audit.md): requirements.txt uses
# unpinned `>=` versions, so the next install pulls "the latest" of every
# transitive dep at install time. A lockfile pins exact versions; ideally
# with sha256 hashes, so a compromised PyPI mirror can't ship a different
# wheel under the same name.
#
# Tool preference (highest first):
#   1. uv     — `uv pip compile requirements.txt -o requirements.lock --generate-hashes`
#   2. pip-compile — `pip-compile --generate-hashes -o requirements.lock requirements.txt`
#   3. pip freeze — fallback; loses hashes but pins versions.
#
# Usage:
#   scripts/lock-deps.sh

set -e

cd "$(dirname "$0")/.."

if command -v uv >/dev/null 2>&1; then
    echo "▸ Using uv"
    uv pip compile requirements.txt -o requirements.lock --generate-hashes
    echo "  → wrote requirements.lock with hashes"
elif command -v pip-compile >/dev/null 2>&1; then
    echo "▸ Using pip-compile"
    pip-compile --generate-hashes --output-file requirements.lock requirements.txt
    echo "  → wrote requirements.lock with hashes"
else
    echo "▸ Falling back to pip freeze (no hashes)"
    echo "  install uv or pip-tools for hash-pinned locks:"
    echo "    pip install uv      # OR"
    echo "    pip install pip-tools"
    pip freeze > requirements.lock
    echo "  → wrote requirements.lock"
fi

#!/usr/bin/env bash
# Package src/ into Booki.alfredworkflow (a zip with files at the archive
# root — Alfred's expected layout). Double-click the output to install.

set -euo pipefail
cd "$(dirname "$0")"

OUT="Booki.alfredworkflow"
rm -f "$OUT"

# Sanity check
plutil -lint src/info.plist >/dev/null

(
  cd src
  # -X strips extra metadata (no .DS_Store, no resource forks).
  # -q so the zip command is quiet — Alfred only cares about the contents.
  zip -X -q -r "../$OUT" . -x ".*" "__pycache__/*"
)

echo "✓ Built $OUT ($(du -h "$OUT" | cut -f1))"
echo "  Double-click it to install in Alfred."

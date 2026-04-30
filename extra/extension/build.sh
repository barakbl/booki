#!/usr/bin/env bash
# Assemble per-platform extension bundles under dist/.
# Each bundle = src/shared/* + src/<platform>/*. No bundler, no transpiler —
# the build is literally `cp`.

set -euo pipefail
cd "$(dirname "$0")"

rm -rf dist
mkdir -p dist/chrome dist/firefox

for plat in chrome firefox; do
  cp -R src/shared/. "dist/$plat/"
  cp src/$plat/manifest.json "dist/$plat/"
  cp src/$plat/platform.js "dist/$plat/"
done

echo "✓ Built dist/chrome/ and dist/firefox/"
echo "  Load dist/chrome/  → chrome://extensions/  (Developer mode → Load unpacked)"
echo "  Load dist/firefox/ → about:debugging       (This Firefox → Load Temporary Add-on)"

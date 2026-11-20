#!/usr/bin/env bash
# Re-vendor the browser libraries from the npm registry at pinned versions.
# The service loads nothing from a CDN, so these files are committed; after running this,
# update the sha256 values in src/lymphflow/web/vendor/VENDOR.json (a unit test checks them).
set -euo pipefail
PLOTLY=4.3.0
HTM=3.1.1
PREACT=10.19.3     # licence text only; htm's standalone build bundles preact
dest="$(cd "$(dirname "$0")/.." && pwd)/src/lymphflow/web/vendor"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
cd "$work"
npm pack --silent "plotly.js-cartesian-dist-min@$PLOTLY" "htm@$HTM" "preact@$PREACT" >/dev/null
for t in *.tgz; do mkdir "x_${t%.tgz}" && tar -xzf "$t" -C "x_${t%.tgz}"; done
cp x_plotly*/package/plotly-cartesian.min.js "$dest/plotly-cartesian.min.js"
cp x_plotly*/package/LICENSE "$dest/LICENSE.plotly.txt"
cp x_htm-*/package/preact/standalone.module.js "$dest/htm-preact-standalone.module.js"
cp x_htm-*/package/LICENSE "$dest/LICENSE.htm.txt"
cp x_preact-*/package/LICENSE "$dest/LICENSE.preact.txt"
sha256sum "$dest/plotly-cartesian.min.js" "$dest/htm-preact-standalone.module.js"

#!/usr/bin/env bash
# Downloads the JPL DE421 planetary ephemeris (~16 MB) that skyfield needs to work
# out where the Sun, Moon and planets are. Public domain, from NASA JPL / NAIF.
set -euo pipefail
DEST="$(cd "$(dirname "$0")/.." && pwd)/skills/seestar/data"
mkdir -p "$DEST"
if [ -f "$DEST/de421.bsp" ]; then
  echo "already have $DEST/de421.bsp"; exit 0
fi
curl -fL --progress-bar \
  https://naif.jpl.nasa.gov/pub/naif/generic_kernels/spk/planets/de421.bsp \
  -o "$DEST/de421.bsp"
echo "saved $DEST/de421.bsp"

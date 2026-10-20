#!/bin/bash
# Package the web remote's home screen as a Samsung TV app (.wgt) and install it.
# Usage: ./build.sh [cert-profile] [tv-ip]
set -euo pipefail
cd "$(dirname "$0")"
PROFILE="${1:-GlassHome}"
TV="${2:-${SAMSUNG_TV_IP:-}}"
TIZEN="$HOME/tizen-studio/tools/ide/bin/tizen"
SDB="$HOME/tizen-studio/tools/sdb"

rm -rf build && mkdir build
cp config.xml build/
cp ../remote.html build/index.html
cp ../remote-icon-512.png build/icon.png
"$TIZEN" package -t wgt -s "$PROFILE" -- build
# The package is named after <name>; a space in it makes "tizen install" fail
WGT="build/GlassHome.wgt"
mv build/*.wgt "$WGT"
echo "Built $WGT"

if [ -n "$TV" ]; then
  "$SDB" connect "$TV"
  "$TIZEN" install -n "$(basename "$WGT")" -s "$TV:26101" -- build
fi

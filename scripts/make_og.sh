#!/bin/sh
# Renders docs/og/og.html to site/og.png (1200x630 social preview) with headless Chrome.
cd "$(dirname "$0")/.."
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
"$CHROME" --headless=new --disable-gpu --hide-scrollbars --force-device-scale-factor=1 \
  --window-size=1200,630 --screenshot="$PWD/site/og.png" "file://$PWD/docs/og/og.html" 2>/dev/null
ls -l site/og.png

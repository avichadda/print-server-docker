#!/bin/sh
set -eu

export LC_ALL=C

lpstat -r >/dev/null
python3 -c "
import urllib.request
with urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=3) as response:
    assert response.status == 200
"

if [ -n "${PRINTER_URI:-}" ]; then
  printer_name=${PRINTER_NAME:-Canon_G3010}
  lpstat -p "$printer_name" >/dev/null

  configured_uri=$(
    lpstat -v "$printer_name" \
      | sed -n "s/^device for ${printer_name}: //p"
  )
  [ "$configured_uri" = "$PRINTER_URI" ]
fi

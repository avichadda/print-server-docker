#!/bin/sh
set -eu

export LC_ALL=C

printer_name=${PRINTER_NAME:-Canon_G3010}
printer_uri=${PRINTER_URI:-}
printer_model=${PRINTER_MODEL:-everywhere}
printer_info=${PRINTER_INFO:-Canon G3010}
printer_location=${PRINTER_LOCATION:-}
enable_avahi=${ENABLE_AVAHI:-true}
cups_start_timeout=${CUPS_START_TIMEOUT:-20}

case "$printer_name" in
  ""|*[!A-Za-z0-9._-]*)
    echo "ERROR: PRINTER_NAME may contain only letters, numbers, periods, underscores, and hyphens." >&2
    exit 64
    ;;
esac

case "$enable_avahi" in
  true|false) ;;
  *)
    echo "ERROR: ENABLE_AVAHI must be either 'true' or 'false'." >&2
    exit 64
    ;;
esac

case "$cups_start_timeout" in
  ""|*[!0-9]*)
    echo "ERROR: CUPS_START_TIMEOUT must be a positive integer." >&2
    exit 64
    ;;
esac

if [ "$cups_start_timeout" -eq 0 ]; then
  echo "ERROR: CUPS_START_TIMEOUT must be greater than zero." >&2
  exit 64
fi

if [ -n "$printer_uri" ]; then
  case "$printer_uri" in
    ipp://*|ipps://*) ;;
    *)
      echo "ERROR: PRINTER_URI must use the ipp:// or ipps:// scheme." >&2
      exit 64
      ;;
  esac
fi

mkdir -p /run/cups /var/log/cups /var/spool/cups
rm -f /run/cups/cupsd.pid

avahi_pid=
dbus_pid=
cups_pid=
web_pid=

stop_services() {
  status=$?
  trap - EXIT INT TERM

  for pid in "$web_pid" "$cups_pid" "$avahi_pid" "$dbus_pid"; do
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      kill -TERM "$pid" 2>/dev/null || true
    fi
  done

  for pid in "$web_pid" "$cups_pid" "$avahi_pid" "$dbus_pid"; do
    if [ -n "$pid" ]; then
      wait "$pid" 2>/dev/null || true
    fi
  done

  exit "$status"
}

trap stop_services EXIT INT TERM

if [ "$enable_avahi" = "true" ]; then
  mkdir -p /run/dbus
  rm -f /run/dbus/pid
  dbus-daemon --system --nofork --nopidfile &
  dbus_pid=$!

  waited=0
  while [ ! -S /run/dbus/system_bus_socket ]; do
    if ! kill -0 "$dbus_pid" 2>/dev/null; then
      echo "ERROR: D-Bus exited before creating its system socket." >&2
      exit 1
    fi
    if [ "$waited" -ge "$cups_start_timeout" ]; then
      echo "ERROR: Timed out waiting for the D-Bus system socket." >&2
      exit 1
    fi
    sleep 1
    waited=$((waited + 1))
  done

  avahi-daemon --no-chroot --no-drop-root &
  avahi_pid=$!
fi

/usr/sbin/cupsd -f &
cups_pid=$!

waited=0
until lpstat -r >/dev/null 2>&1; do
  if ! kill -0 "$cups_pid" 2>/dev/null; then
    wait "$cups_pid" || true
    echo "ERROR: CUPS exited before it became ready." >&2
    exit 1
  fi
  if [ "$waited" -ge "$cups_start_timeout" ]; then
    echo "ERROR: Timed out waiting for CUPS to become ready." >&2
    exit 1
  fi
  sleep 1
  waited=$((waited + 1))
done

if [ -n "$printer_uri" ]; then
  configured_uri=$(
    lpstat -v "$printer_name" 2>/dev/null \
      | sed -n "s/^device for ${printer_name}: //p" \
      || true
  )

  if [ "$configured_uri" != "$printer_uri" ]; then
    echo "Configuring printer '$printer_name' at '$printer_uri'."
    if ! lpadmin \
      -p "$printer_name" \
      -E \
      -v "$printer_uri" \
      -m "$printer_model" \
      -D "$printer_info" \
      -L "$printer_location" \
      -o printer-is-shared=true; then
      echo "ERROR: Failed to configure '$printer_name'. Ensure the printer is online and its IPP URI is correct." >&2
      exit 1
    fi
  else
    lpadmin -p "$printer_name" -o printer-is-shared=true
  fi

  cupsaccept "$printer_name"
  cupsenable "$printer_name"
  lpadmin -d "$printer_name"
  echo "Printer '$printer_name' is enabled, shared, and set as the default."
else
  echo "PRINTER_URI is empty; use the setup interface to add a printer."
fi

waitress-serve --listen=0.0.0.0:8080 manager.app:app &
web_pid=$!

echo "Print Server setup interface is available on port 8080."

while kill -0 "$cups_pid" 2>/dev/null && kill -0 "$web_pid" 2>/dev/null; do
  sleep 2
done

if ! kill -0 "$cups_pid" 2>/dev/null; then
  wait "$cups_pid" || true
  echo "ERROR: CUPS stopped unexpectedly." >&2
else
  wait "$web_pid" || true
  echo "ERROR: The setup interface stopped unexpectedly." >&2
fi
exit 1

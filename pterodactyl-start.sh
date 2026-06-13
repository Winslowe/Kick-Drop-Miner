#!/bin/sh
set -eu

export KDM_DATA_DIR="${KDM_DATA_DIR:-/home/container/data}"
export KDM_SYSTEM_CHROMIUM=1
export CHROME_BINARY="${CHROME_BINARY:-/usr/bin/chromium}"
export CHROMEDRIVER_PATH="${CHROMEDRIVER_PATH:-/usr/bin/chromedriver}"
export FIREFOX_BINARY="${FIREFOX_BINARY:-/usr/bin/firefox-esr}"
export GECKODRIVER_PATH="${GECKODRIVER_PATH:-/usr/local/bin/geckodriver}"
export HOME=/home/container
export XDG_CONFIG_HOME="$KDM_DATA_DIR/xdg/config"
export XDG_CACHE_HOME="$KDM_DATA_DIR/xdg/cache"
export DISPLAY="${DISPLAY:-:99}"
runtime_root="$KDM_DATA_DIR/runtime"
runtime_debs="$KDM_DATA_DIR/runtime-debs"

mkdir -p \
  "$KDM_DATA_DIR/cookies" \
  "$KDM_DATA_DIR/chrome_data" \
  "$XDG_CONFIG_HOME" \
  "$XDG_CACHE_HOME"

if [ -d "$runtime_debs" ]; then
  rm -rf "$runtime_root"
  mkdir -p "$runtime_root"
  for package in "$runtime_debs"/*.deb; do
    [ -f "$package" ] || continue
    dpkg-deb -x "$package" "$runtime_root"
  done
  export PATH="$runtime_root/usr/bin:$PATH"
  export LD_LIBRARY_PATH="$runtime_root/usr/lib/aarch64-linux-gnu:$runtime_root/lib/aarch64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
  if [ -d "$runtime_root/usr/lib/aarch64-linux-gnu/dri" ]; then
    export LIBGL_DRIVERS_PATH="$runtime_root/usr/lib/aarch64-linux-gnu/dri"
    export LIBGL_ALWAYS_SOFTWARE=1
  fi
fi

rm -f "/tmp/.X${DISPLAY#:}-lock"
Xvfb "$DISPLAY" \
  -screen 0 "${KDM_XVFB_SCREEN:-1280x800x24}" \
  +extension GLX \
  +render \
  -noreset \
  -nolisten tcp \
  -ac \
  > "$KDM_DATA_DIR/xvfb.log" 2>&1 &

display_number="${DISPLAY#:}"
attempt=0
while [ ! -S "/tmp/.X11-unix/X${display_number}" ]; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 50 ]; then
    echo "Sanal ekran başlatılamadı." >&2
    exit 1
  fi
  sleep 0.1
done

echo "Checking for updates from GitHub..."
git pull origin main || echo "git pull failed, continuing with existing code"

echo "Started successfully"
exec python -m uvicorn webapp:app \
  --host 0.0.0.0 \
  --port "${SERVER_PORT:-8080}" \
  --proxy-headers \
  --forwarded-allow-ips="*"

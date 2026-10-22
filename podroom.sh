#!/usr/bin/env bash
# Start the Podroom server if it is not already running, then open the app window.
DIR="$(cd "$(dirname "$0")" && pwd)"
URL="http://127.0.0.1:${PODROOM_PORT:-${PHONON_PORT:-7860}}"
# The app window's own browser profile; an install from before the rename keeps its old one.
PROFILE="$HOME/.config/podroom-chrome"
[ -d "$HOME/.config/phonon-tts-chrome" ] && [ ! -d "$PROFILE" ] && PROFILE="$HOME/.config/phonon-tts-chrome"

STARTED=""
if ! curl -fs "$URL/api/voices" -o /dev/null --max-time 2; then
  nohup "$DIR/.venv/bin/python" "$DIR/server.py" >"$DIR/server.log" 2>&1 &
  STARTED=$!
  for _ in $(seq 1 50); do
    curl -fs "$URL/" -o /dev/null --max-time 1 && break
    sleep 0.2
  done
fi

# An app window without tabs or address bar; fall back to the default browser.
if command -v google-chrome >/dev/null; then
  google-chrome --app="$URL" --class=Podroom --user-data-dir="$PROFILE" --window-size=1360,900
  # The window is closed: stop the server this launch started. One that was already running is left alone.
  [ -n "$STARTED" ] && kill "$STARTED" 2>/dev/null
  exit 0
else
  exec xdg-open "$URL"  # a browser tab gives no signal when it closes, so the server keeps running
fi

#!/usr/bin/env bash
# Start the Phonon TTS server if it is not already running, then open the app window.
DIR="$(cd "$(dirname "$0")" && pwd)"
URL="http://127.0.0.1:${PHONON_PORT:-7860}"

if ! curl -fs "$URL/api/voices" -o /dev/null --max-time 2; then
  nohup "$DIR/.venv/bin/python" "$DIR/server.py" >"$DIR/server.log" 2>&1 &
  for _ in $(seq 1 50); do
    curl -fs "$URL/" -o /dev/null --max-time 1 && break
    sleep 0.2
  done
fi

# An app window without tabs or address bar; fall back to the default browser.
if command -v google-chrome >/dev/null; then
  exec google-chrome --app="$URL" --class=PhononTTS --user-data-dir="$HOME/.config/phonon-tts-chrome" --window-size=1360,900
else
  exec xdg-open "$URL"
fi

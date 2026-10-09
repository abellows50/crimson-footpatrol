#!/bin/bash
# Double-click on a Mac (or run ./start.command) to start the Crimson EMS dispatch monitor.
cd "$(dirname "$0")"
if ! .venv/bin/python -c 'import faster_whisper, playwright' >/dev/null 2>&1; then
  echo "Not set up yet (or the folder was moved): running install.command first..."
  ./install.command || exit 1
fi
[ -f settings.env ] && set -a && . ./settings.env && set +a
IP=$(ipconfig getifaddr en0 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}')
echo "Dashboard on this computer: http://localhost:${PORT:-8080}"
[ -n "$IP" ] && echo "From phones on the same network: http://$IP:${PORT:-8080}"
# keep the Mac awake while the monitor runs
if command -v caffeinate >/dev/null; then
  exec caffeinate -dimsu ./.venv/bin/python server.py "$@"
else
  exec ./.venv/bin/python server.py "$@"
fi

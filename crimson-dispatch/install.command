#!/bin/bash
# Crimson EMS Dispatch Watch: one-time setup on a Mac. Double-click it (or run ./install.command).
# Safe to run again any time: it only fixes or adds what's missing, and never touches logs/ or your settings.
set -u
cd "$(dirname "$0")"
DIR="$(pwd)"
ok()   { printf "  \033[32m✓\033[0m %s\n" "$1"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$1"; }
fail() { printf "  \033[31m✗\033[0m %s\n" "$1"; read -r -p "Press Enter to close. " _; exit 1; }
step() { printf "\n\033[1m%s\033[0m\n" "$1"; }

echo "Crimson EMS Dispatch Watch: setup"
echo "Folder: $DIR"

# ------------------------------------------------------------------ 1. this computer
step "1/6  Checking this computer"
[ "$(uname)" = "Darwin" ] || warn "This script is written for macOS; on Linux it should mostly work, but it is untested."
ARM=0; [ "$(uname -m)" = "arm64" ] && ARM=1
if [ $ARM = 1 ]; then ok "Apple Silicon: Whisper will run on the Mac's GPU (large-v3-turbo)"
else warn "Not an Apple Silicon Mac: no GPU Whisper, so it uses the smaller small.en model (still fine, a bit less accurate)"; fi

# ------------------------------------------------------------------ 2. Python
step "2/6  Python"
PY=""
for c in /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    PY="$(command -v "$c")"; break
  fi
done
[ -n "$PY" ] || fail "Python 3.10 or newer is needed. Install it from https://www.python.org/downloads/ (or 'brew install python'), then run this again."
ok "$("$PY" --version) ($PY)"

# ------------------------------------------------------------------ 3. virtual environment
step "3/6  App environment (.venv)"
if [ -d .venv ]; then
  # A .venv made in another folder (e.g. after moving the app) half-works: rebuild it.
  MADE_IN="$(sed -n 's/^command = .* -m venv //p' .venv/pyvenv.cfg 2>/dev/null)"
  if ! .venv/bin/python -c 'import sys' >/dev/null 2>&1; then
    warn "The existing .venv is broken; rebuilding it."; mv .venv ".venv.old.$(date +%s)"
  elif [ -n "$MADE_IN" ] && [ "$MADE_IN" != "$DIR/.venv" ]; then
    warn "The .venv was made at $MADE_IN (the app was moved); rebuilding it here."; mv .venv ".venv.old.$(date +%s)"
  fi
fi
if [ ! -d .venv ]; then
  "$PY" -m venv .venv || fail "Couldn't create the Python environment."
  ok "Created .venv"
else
  ok ".venv is fine"
fi
VPY="$DIR/.venv/bin/python"
echo "  Installing / updating packages (first time: a few minutes)..."
"$VPY" -m pip install -q --upgrade pip || fail "pip upgrade failed (internet?)"
"$VPY" -m pip install -q -r requirements.txt || fail "Installing requirements failed. Scroll up for the error."
ok "Packages installed"
ls .venv.old.* >/dev/null 2>&1 && warn "An old environment was kept as .venv.old.* : delete it to free space once everything works."

# ------------------------------------------------------------------ 4. browser for OpenMHz
step "4/6  Browser for OpenMHz"
CHROME=0
if [ -d "/Applications/Google Chrome.app" ] || [ -d "$HOME/Applications/Google Chrome.app" ]; then
  CHROME=1; ok "Google Chrome found (used to get past OpenMHz's Cloudflare check)"
else
  warn "Google Chrome isn't installed. OpenMHz blocks Playwright's built-in browser, so install Chrome from https://www.google.com/chrome/ and run this again."
  "$VPY" -m playwright install chromium >/dev/null 2>&1 && ok "Installed Playwright's Chromium as a fallback"
fi

# ------------------------------------------------------------------ 5. speech model
step "5/6  Speech model"
if [ $ARM = 1 ]; then
  if [ -f models/large-v3-turbo-mlx/config.json ] && ls models/large-v3-turbo-mlx/weights.* >/dev/null 2>&1; then
    ok "models/large-v3-turbo-mlx already downloaded"
  else
    echo "  Downloading Whisper large-v3-turbo for the Mac GPU (~1.6 GB)..."
    "$VPY" - <<'PYEOF' || fail "Model download failed (internet?). Run this again to resume."
from huggingface_hub import snapshot_download
snapshot_download("mlx-community/whisper-large-v3-turbo", local_dir="models/large-v3-turbo-mlx",
                  allow_patterns=["config.json", "weights.*"])
PYEOF
    ok "Downloaded to models/large-v3-turbo-mlx"
  fi
else
  ok "small.en downloads automatically the first time the server starts (~500 MB)"
fi

# ------------------------------------------------------------------ 6. settings + launchers
step "6/6  Settings"
if [ ! -f settings.env ]; then
  cp settings.env.example settings.env
  ok "Created settings.env from the example (phone alerts etc. are set there)"
fi
add_setting() {   # add KEY=VALUE to settings.env unless the key is already set (uncommented)
  grep -qE "^[[:space:]]*$1=" settings.env || { printf '\n%s=%s\n' "$1" "$2" >> settings.env; ok "settings.env: $1=$2"; }
}
if [ $CHROME = 1 ]; then add_setting BROWSER_CHANNEL chrome; add_setting HEADFUL 1; fi
chmod +x start.command train.command install.command 2>/dev/null
xattr -d com.apple.quarantine start.command train.command install.command 2>/dev/null
ok "start.command and train.command are ready to double-click"
if command -v claude >/dev/null 2>&1; then ok "Claude Code found: AI reading of dispatches will be on"
else warn "The 'claude' command isn't installed, so AI summaries are off (everything else works). See https://claude.com/claude-code"; fi

printf "\n\033[1mAll set.\033[0m Double-click start.command, then open http://localhost:8080\n"
echo "Tip: set your crew's base in Settings → Map & base, and phone alerts (NTFY_TOPIC) in settings.env."
read -r -p "Press Enter to close. " _

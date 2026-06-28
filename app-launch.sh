#!/usr/bin/env bash
# App launcher used by the Windows desktop shortcut (see windows/). Idempotent:
# starts the ApartmentFinder server only if it isn't already running, then execs it
# in the FOREGROUND so the hidden `wsl.exe` the shortcut spawned keeps the WSL session
# — and thus the server — alive. Runs WITHOUT --reload and bound to localhost: this is
# for *using* the app, not developing it (use ./run.sh for that).
cd "$(dirname "$0")" || exit 1

# The [u] bracket keeps this pattern from matching the launcher's own command line
# (a literal "uvicorn app.main:app" in argv would otherwise self-match).
if pgrep -f "[u]vicorn app.main:app" >/dev/null 2>&1; then
  exit 0   # already up — nothing to do
fi

source .venv/bin/activate
mkdir -p data
exec uvicorn app.main:app --host 127.0.0.1 --port 8000 >>data/server.log 2>&1

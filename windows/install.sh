#!/usr/bin/env bash
# One-time (re-runnable) setup for the Windows desktop launcher. Copies the launcher
# scripts + icon into %LOCALAPPDATA%\ApartmentFinder and creates the Desktop shortcuts:
#   • "ApartmentFinder"      — starts the server (if needed) and opens it in the browser
#   • "Stop ApartmentFinder" — shuts the server down
# Run it from WSL:  ./windows/install.sh
set -euo pipefail
cd "$(dirname "$0")"   # the windows/ dir

# Resolve %LOCALAPPDATA% on the Windows side, then a WSL path we can copy into.
appdir_win="$(powershell.exe -NoProfile -Command '$env:LOCALAPPDATA' | tr -d '\r')\\ApartmentFinder"
appdir_wsl="$(wslpath "$appdir_win")"
mkdir -p "$appdir_wsl"

cp ApartmentFinder.vbs Stop-ApartmentFinder.vbs app.ico create-shortcuts.ps1 "$appdir_wsl/"
echo "Copied launcher files to: $appdir_win"

# Create the Desktop shortcuts (the .ps1 runs from its now-Windows-local location).
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$appdir_win\\create-shortcuts.ps1"
echo "Done — look for the 'ApartmentFinder' icon on your Desktop."

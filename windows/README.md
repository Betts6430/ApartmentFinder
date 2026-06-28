# ApartmentFinder — Windows desktop launcher

Run the app like a normal program — no terminal.

## Use it

- **ApartmentFinder** (Desktop icon) — starts the app and opens it in your browser.
  First launch from cold takes a few seconds while the server boots; after that it's
  instant. If it's already running, it just opens the browser.
- **Stop ApartmentFinder** (Desktop icon) — shuts the server down.

The app lives at <http://localhost:8000> — you can bookmark that too.

## (Re)install the shortcuts

From a WSL/Ubuntu terminal in the project folder:

```bash
./windows/install.sh
```

This copies the launcher scripts + icon into `%LOCALAPPDATA%\ApartmentFinder` and
(re)creates the two Desktop shortcuts. Safe to re-run.

## How it works

- `ApartmentFinder.vbs` runs (hidden) `wsl.exe … ./app-launch.sh`, waits for the server,
  then opens your default browser. `app-launch.sh` starts **uvicorn on 127.0.0.1:8000**
  only if it isn't already running (no `--reload` — this is for using the app).
- `Stop-ApartmentFinder.vbs` stops the server (`pkill`).
- The server runs inside WSL, so it stops if you shut down/restart Windows — just
  click the icon again. Editing the code? Use `./run.sh` instead (hot-reload), or
  restart via the icon to pick up changes.

> Paths are hard-coded for this machine (WSL distro `Ubuntu`, project at
> `/home/betts6430/claude_code/ApartmentFinder`). Edit the `.vbs` files if those change.

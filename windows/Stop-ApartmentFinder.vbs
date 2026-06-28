' ApartmentFinder — stop the local server (double-clicked via the Desktop shortcut).
Option Explicit
Dim sh : Set sh = CreateObject("WScript.Shell")
' The [u] bracket keeps pkill's own command line from matching itself; only the real
' uvicorn process matches. Wait for it to finish (True), then confirm.
sh.Run "wsl.exe -d Ubuntu bash -c ""pkill -f '[u]vicorn app.main:app'""", 0, True
MsgBox "ApartmentFinder has been stopped.", 64, "ApartmentFinder"

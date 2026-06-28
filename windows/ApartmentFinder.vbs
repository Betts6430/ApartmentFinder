' ApartmentFinder launcher (double-clicked via the Desktop shortcut).
' Starts the local server inside WSL if it isn't already running, waits for it to
' come up, then opens it in your default browser. Runs with no visible terminal.
Option Explicit
Dim sh, http, i, isUp
Set sh = CreateObject("WScript.Shell")

' Start the server in WSL: hidden window (0), don't wait (False). app-launch.sh is
' idempotent, so a second launch just no-ops and we go straight to opening the browser.
sh.Run "wsl.exe -d Ubuntu bash -c ""cd /home/betts6430/claude_code/ApartmentFinder && ./app-launch.sh""", 0, False

' Poll up to ~25s for the server to answer before opening the browser, so the first
' page doesn't load before uvicorn has bound the port (cold WSL start).
isUp = False
For i = 1 To 50
  On Error Resume Next
  Set http = CreateObject("MSXML2.XMLHTTP")
  http.Open "GET", "http://localhost:8000/", False
  http.Send
  If Err.Number = 0 And http.Status = 200 Then isUp = True
  On Error GoTo 0
  If isUp Then Exit For
  WScript.Sleep 500
Next

sh.Run "cmd /c start """" ""http://localhost:8000/""", 0, False

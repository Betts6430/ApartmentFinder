# Creates the ApartmentFinder Desktop shortcuts, pointing at the launcher scripts in
# %LOCALAPPDATA%\ApartmentFinder. Invoked by windows/install.sh (re-runnable).
$ErrorActionPreference = 'Stop'
$appdir  = Join-Path $env:LOCALAPPDATA 'ApartmentFinder'
$desktop = [Environment]::GetFolderPath('Desktop')
$wscript = Join-Path $env:WINDIR 'System32\wscript.exe'
$icon    = Join-Path $appdir 'app.ico'
$ws = New-Object -ComObject WScript.Shell

function New-AppShortcut($name, $vbs, $desc) {
  $s = $ws.CreateShortcut((Join-Path $desktop $name))
  $s.TargetPath = $wscript
  $s.Arguments = '"' + (Join-Path $appdir $vbs) + '"'
  $s.IconLocation = $icon
  $s.WorkingDirectory = $appdir
  $s.Description = $desc
  $s.Save()
}

New-AppShortcut 'ApartmentFinder.lnk'      'ApartmentFinder.vbs'      'Open ApartmentFinder'
New-AppShortcut 'Stop ApartmentFinder.lnk' 'Stop-ApartmentFinder.vbs' 'Stop the ApartmentFinder server'
Write-Output ('Shortcuts created on: ' + $desktop)

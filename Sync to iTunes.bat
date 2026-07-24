@echo off
:: Double-click to add new downloads to iTunes and rebuild the playlists.
:: Runs itunes_sync.ps1 next to this file. Requires the CLASSIC desktop iTunes,
:: opened once by hand (first-run dialog dismissed), with "Copy files to iTunes
:: Media folder" turned OFF (Edit > Preferences > Advanced).

setlocal
set "HERE=%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%HERE%itunes_sync.ps1"
echo.
pause

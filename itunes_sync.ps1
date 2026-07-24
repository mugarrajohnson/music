<#
================================================================
  iTunes Sync  -  add downloaded audio + (re)build playlists
================================================================
  1. Adds every MP3 under <audio> to the iTunes library IN PLACE.
  2. Rebuilds one iTunes playlist per .m3u8 file - WITHOUT creating duplicates.
     Any existing playlist(s) of the same name are deleted first, then one fresh
     copy is created. So re-running only refreshes; it never stacks duplicates,
     and it cleans up duplicates left by earlier runs.

  Album Extras (whole albums pulled for songs you own) are added to the library too.
  They are NOT in an .m3u8, but they carry real Album + Genre tags, so they show up
  automatically in iTunes' Albums view and Genre browser next to the song you had.

  REQUIREMENTS (one-time):
    1. Classic desktop iTunes (from apple.com) - the Store version cannot be scripted.
    2. Open iTunes once by hand and click through the first-run/licence screens.
    3. Edit > Preferences > Advanced:
         [ ] Copy files to iTunes Media folder when adding to library   <- UNCHECK
         [ ] Keep iTunes Media folder organized                          <- UNCHECK
       Otherwise iTunes MOVES your files and breaks the playlists and the ledger.

  USAGE:   powershell -ExecutionPolicy Bypass -File itunes_sync.ps1
================================================================
#>

param(
    [string]$AudioRoot    = "D:\Downloads\audio",
    [string]$PlaylistsDir = "D:\Downloads\playlists",
    [int]   $BatchSize    = 200
)

$ErrorActionPreference = "Stop"

function Wait-Status($status) {
    if ($null -eq $status) { return }
    while ($status.InProgress) { Start-Sleep -Milliseconds 150 }
}

Write-Host "Connecting to iTunes..." -ForegroundColor Cyan
try {
    $iTunes = New-Object -ComObject iTunes.Application
} catch {
    Write-Host "ERROR: could not connect to iTunes COM." -ForegroundColor Red
    Write-Host "  - Install the CLASSIC desktop iTunes (apple.com), not the Store version."
    Write-Host "  - Open iTunes once by hand and dismiss the first-run dialog, then retry."
    exit 1
}
$lib = $iTunes.LibraryPlaylist
Write-Host ("Connected. iTunes {0} - library has {1} tracks." -f $iTunes.Version, $lib.Tracks.Count)

# ---------------------------------------------------------------
# Phase 1: add all audio to the library (in place; iTunes dedups by file location)
# ---------------------------------------------------------------
if (-not (Test-Path $AudioRoot)) { Write-Host "Audio root not found: $AudioRoot" -ForegroundColor Red; exit 1 }
$files = Get-ChildItem -Path $AudioRoot -Recurse -File -Filter *.mp3
Write-Host ("`nPhase 1: adding {0} MP3 file(s) to the library..." -f $files.Count) -ForegroundColor Cyan
$done = 0
for ($i = 0; $i -lt $files.Count; $i += $BatchSize) {
    $batch = $files[$i..([Math]::Min($i + $BatchSize - 1, $files.Count - 1))] | ForEach-Object { $_.FullName }
    try { Wait-Status ($lib.AddFiles([string[]]$batch)) } catch {
        Write-Host ("  batch at {0} failed: {1}" -f $i, $_.Exception.Message) -ForegroundColor Yellow
    }
    $done += $batch.Count
    Write-Progress -Activity "Adding to library" -Status "$done / $($files.Count)" `
                   -PercentComplete ([int](100 * $done / [Math]::Max($files.Count,1)))
}
Write-Progress -Activity "Adding to library" -Completed
Write-Host ("  library now has {0} tracks." -f $lib.Tracks.Count) -ForegroundColor Green

# ---------------------------------------------------------------
# Phase 2: rebuild playlists without duplicates
# ---------------------------------------------------------------
$m3us = Get-ChildItem -Path $PlaylistsDir -File | Where-Object { $_.Extension -in ".m3u8", ".m3u" }
Write-Host ("`nPhase 2: rebuilding {0} playlist(s)..." -f $m3us.Count) -ForegroundColor Cyan
$managed = @{}
foreach ($f in $m3us) { $managed[[IO.Path]::GetFileNameWithoutExtension($f.Name)] = $true }

# Delete EVERY existing playlist whose name we manage (iterate by index, backwards, so
# deletions don't shift the ones we haven't looked at yet). Smart playlists are left
# alone. This is what removes the duplicates that earlier runs created.
$pls = $iTunes.LibrarySource.Playlists
$removed = 0
for ($i = $pls.Count; $i -ge 1; $i--) {
    $pl = $pls.Item($i)
    if ($null -eq $pl) { continue }
    $name = $pl.Name
    if (-not $managed.ContainsKey($name)) { continue }
    $isSmart = $false
    try { $isSmart = [bool]$pl.Smart } catch {}
    if ($isSmart) { continue }
    try { $pl.Delete(); $removed++ } catch {}
}
Write-Host ("  removed {0} existing/duplicate playlist(s) with managed names." -f $removed)

# Now create exactly one fresh playlist per .m3u8.
$built = 0; $totalTracks = 0
foreach ($m3u in $m3us) {
    $name  = [IO.Path]::GetFileNameWithoutExtension($m3u.Name)
    $paths = Get-Content -LiteralPath $m3u.FullName -Encoding UTF8 |
             Where-Object { $_ -and -not $_.StartsWith("#") } |
             Where-Object { Test-Path -LiteralPath $_ }
    if (-not $paths) { continue }

    $playlist = $iTunes.CreatePlaylist($name)
    for ($i = 0; $i -lt $paths.Count; $i += $BatchSize) {
        $batch = $paths[$i..([Math]::Min($i + $BatchSize - 1, $paths.Count - 1))]
        try { Wait-Status ($playlist.AddFiles([string[]]$batch)) } catch {}
    }
    $built++; $totalTracks += $paths.Count
    Write-Host ("  {0}  ->  {1} track(s)" -f $name, $paths.Count)
}

Write-Host "`n============================================================" -ForegroundColor Green
Write-Host ("  Duplicates cleaned : {0}" -f $removed)
Write-Host ("  Playlists rebuilt  : {0}" -f $built)
Write-Host ("  Track slots added  : {0}" -f $totalTracks)
Write-Host ("  Library tracks     : {0}" -f $lib.Tracks.Count)
Write-Host "============================================================" -ForegroundColor Green
Write-Host "  Album Extras are in the library too - browse them by Album or Genre in iTunes."

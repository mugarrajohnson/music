@echo off
setlocal

:: ============================================================
::  Music Downloader Runner
::
::  Double-click this file OR let Task Scheduler run it.
::  Fully self-locating - no hardcoded paths.
:: ============================================================

:: --- Self-locate: always resolve from the folder this .bat lives in ---
SET "PROJECT_ROOT=%~dp0"
IF "%PROJECT_ROOT:~-1%"=="\" SET "PROJECT_ROOT=%PROJECT_ROOT:~0,-1%"

SET "LOGS_DIR=%PROJECT_ROOT%\logs"
if not exist "%LOGS_DIR%" mkdir "%LOGS_DIR%"

:: --- Detect an interactive session. UserInteractive is True for a double-click /
::     console, False for a headless Task Scheduler "run whether logged on or not" run.
::     We use it to decide whether to stream to the console or capture to the log file. ---
set "INTERACTIVE=0"
for /f "usebackq delims=" %%I in (
    `powershell -NoProfile -Command "[Environment]::UserInteractive"`
) do if /i "%%I"=="True" set "INTERACTIVE=1"

:: --- Locale-independent timestamp via PowerShell ---
for /f "usebackq" %%T in (
    `powershell -NoProfile -Command "Get-Date -Format 'yyyyMMdd_HHmm'"`
) do SET "RUN_STAMP=%%T"
SET "RUN_LOG=%LOGS_DIR%\run_%RUN_STAMP%.log"

:: --- Locate Python ---
SET "PYTHON="
python --version >nul 2>&1 && SET "PYTHON=python"
if not defined PYTHON (
    py --version >nul 2>&1 && SET "PYTHON=py"
)
if not defined PYTHON (
    echo ERROR: Python not found on PATH. > "%RUN_LOG%"
    echo Install Python from https://python.org and tick "Add to PATH". >> "%RUN_LOG%"
    echo ERROR: Python not found. See: %RUN_LOG%
    if "%INTERACTIVE%"=="1" pause
    exit /b 1
)

:: --- cd to project root (CRITICAL) ---
:: Task Scheduler starts in C:\Windows\System32; without this cd the script would not
:: find its CSVs, cookies.txt, or the downloads/ledger next to it.
cd /d "%PROJECT_ROOT%"
if errorlevel 1 (
    echo ERROR: Cannot cd to "%PROJECT_ROOT%".
    if "%INTERACTIVE%"=="1" pause
    exit /b 1
)

:: --- Header ---
echo ============================================================
echo  Downloader run started : %DATE% %TIME%
echo  Project root : %PROJECT_ROOT%
echo  Run log      : %RUN_LOG%
echo ============================================================
echo.

:: --- Run the pipeline. Interactive streams live; headless captures to the run log,
::     because a scheduled session has no console and would otherwise lose all output. ---
SET "EXIT_CODE=0"
if "%INTERACTIVE%"=="1" (
    call :run_pipeline
) else (
    call :run_pipeline >> "%RUN_LOG%" 2>&1
)

:: --- Result ---
if "%EXIT_CODE%"=="0" (
    echo.
    echo  Downloader completed successfully at %TIME%
    ( echo Downloader completed successfully at %TIME% ) >> "%RUN_LOG%"
) else (
    echo.
    echo  Downloader FAILED - exit code %EXIT_CODE% at %TIME%  ^(see %RUN_LOG%^)
    ( echo Downloader FAILED - exit code %EXIT_CODE% at %TIME% ) >> "%RUN_LOG%"
)
echo ============================================================

if "%INTERACTIVE%"=="1" pause
exit /b %EXIT_CODE%


:: ------------------------------------------------------------
:: Two phases: 1) download the media fast, then 2) enrich genres online. The online
:: art/genre lookups are rate-limited, so they run as their own resumable phase instead
:: of throttling the download. Add a --reart line to also upgrade cover art.
:: --no-browser is required unattended (no desktop for a Selenium window) and is faster
:: interactively too. EXIT_CODE tracks the DOWNLOAD result; enrichment is best-effort.
:: ------------------------------------------------------------
:run_pipeline
echo Downloader run started : %DATE% %TIME%
:: 1) the playlists themselves (the download result is what EXIT_CODE reports)
%PYTHON% -u "%PROJECT_ROOT%\download_from_csv.py" --all --mode audio --workers 5
set "EXIT_CODE=%errorlevel%"
:: 2) complete the albums: pull whole albums for songs you own, a chunk of albums per run.
::    ~3800 albums, so this fills 380/run and works through them over successive runs.
::    Raise --max-albums (or drop it) to go faster; it is fully resumable.
%PYTHON% -u "%PROJECT_ROOT%\download_from_csv.py" --fill-albums all --max-albums 380 --workers 5
:: 3) enrich everything downloaded this run (playlist songs AND new album tracks)
%PYTHON% -u "%PROJECT_ROOT%\download_from_csv.py" --fix-genres
:: %PYTHON% -u "%PROJECT_ROOT%\download_from_csv.py" --reart
:: 4) refresh Grouping tags + .m3u8 files so iTunes stays in sync
%PYTHON% -u "%PROJECT_ROOT%\download_from_csv.py" --group-tags
%PYTHON% -u "%PROJECT_ROOT%\download_from_csv.py" --playlists
exit /b

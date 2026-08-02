@echo off
REM ====================================================================
REM  One-time setup for Playlist Song Finder (double-click once).
REM
REM  Builds a private Python 3.12 environment (.venv) with "uv" so
REM  shazamio works, without changing your system Python or PATH.
REM  Runs step by step and STOPS at the first thing that fails, so the
REM  window stays open showing exactly what went wrong.
REM ====================================================================
setlocal
cd /d "%~dp0"
set "VENV=%~dp0.venv"
set "VPY=%VENV%\Scripts\python.exe"

REM --- bootstrap Python (any version) just to drive uv ------------------
set "PY=python"
where py >nul 2>nul && set "PY=py"

echo ============================================================
echo  STEP 1/6  Checking that Python is available
echo ============================================================
%PY% --version
if errorlevel 1 (
    echo.
    echo [X] Python was not found. Install it from
    echo     https://www.python.org/downloads/  ^(tick "Add to PATH"^),
    echo     then run this setup again.
    goto fail
)

echo.
echo ============================================================
echo  STEP 2/6  Making sure uv is installed
echo ============================================================
%PY% -m uv --version
if not errorlevel 1 goto haveuv
echo   uv not present -- installing it with pip ...
%PY% -m pip install --upgrade uv
%PY% -m uv --version
if not errorlevel 1 goto haveuv
echo.
echo [X] uv still won't run via "%PY% -m uv".
echo     Try installing it directly, then rerun setup:
echo         %PY% -m pip install --user uv
goto fail
:haveuv

echo.
echo ============================================================
echo  STEP 3/6  Downloading a private Python 3.12 (one time)
echo ============================================================
%PY% -m uv python install 3.12
if errorlevel 1 (
    echo.
    echo [X] uv could not download Python 3.12. Check your internet
    echo     connection and rerun setup.
    goto fail
)

echo.
echo ============================================================
echo  STEP 4/6  Building this tool's .venv
echo ============================================================
if exist "%VENV%" (
    echo   Removing old .venv ...
    rmdir /s /q "%VENV%"
)
%PY% -m uv venv --python 3.12 "%VENV%"
if not exist "%VPY%" (
    echo.
    echo [X] .venv was not created. Expected:
    echo     %VPY%
    goto fail
)
echo   Created: %VPY%

echo.
echo ============================================================
echo  STEP 5/6  Installing yt-dlp + shazamio into the .venv
echo ============================================================
%PY% -m uv pip install --python "%VPY%" --upgrade yt-dlp shazamio
if errorlevel 1 (
    echo.
    echo [X] Installing yt-dlp/shazamio failed -- see the error above.
    goto fail
)
echo.
echo   Verifying shazamio actually loads ...
"%VPY%" -c "import shazamio; print('   OK: shazamio works in the .venv')"
if errorlevel 1 (
    echo.
    echo [!] shazamio installed but failed to import -- see error above.
    echo     Text-tracklist mode will still work; only the Shazam fallback
    echo     needs this. You can rerun setup to retry.
)

echo.
echo ============================================================
echo  STEP 6/6  Checking ffmpeg (needed to cut audio clips)
echo ============================================================
where ffmpeg >nul 2>nul
if not errorlevel 1 (
    echo   ffmpeg already on PATH. Good.
) else (
    where winget >nul 2>nul
    if not errorlevel 1 (
        echo   Installing ffmpeg via winget ...
        winget install --id=Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements
        echo   If ffmpeg isn't found later, reopen this window so PATH refreshes.
    ) else (
        echo   winget not available. Install ffmpeg manually:
        echo     https://www.gyan.dev/ffmpeg/builds/  ^(the "release essentials" zip;
        echo     unzip it and add its \bin folder to PATH^).
    )
)

echo.
echo ============================================================
echo  SETUP COMPLETE. Now run "Run Playlist Song Finder.bat".
echo  The app's log should say: [ok] shazamio found.
echo ============================================================
pause
exit /b 0

:fail
echo.
echo ------------------------------------------------------------
echo  SETUP DID NOT FINISH. Nothing on your system was changed.
echo  Copy the text above (especially the [X] line) so it can be
echo  diagnosed.
echo ------------------------------------------------------------
pause
exit /b 1

@echo off
REM ====================================================================
REM  Launches the Playlist Song Finder GUI.  Double-click this file.
REM
REM  Prefers the tool's private .venv (Python 3.12, created by setup.bat)
REM  because that's where shazamio lives. If .venv isn't there yet, it
REM  falls back to your system Python so text-tracklist mode still works.
REM ====================================================================
cd /d "%~dp0"
set "VPY=%~dp0.venv\Scripts\python.exe"

if exist "%VPY%" (
    "%VPY%" "%~dp0playlist_song_finder.py"
    goto done
)

echo (No .venv found -- run setup.bat for Shazam support. Using system Python.)
where py >nul 2>nul
if %errorlevel%==0 (
    py "%~dp0playlist_song_finder.py"
) else (
    python "%~dp0playlist_song_finder.py"
)

:done
if %errorlevel% neq 0 (
    echo.
    echo Something went wrong. If Python isn't installed, get it from
    echo https://www.python.org/downloads/ ^(check "Add to PATH"^),
    echo then run setup.bat once.
    pause
)

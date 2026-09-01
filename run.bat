@echo off
setlocal EnableExtensions
cd /d "%~dp0"

title Cursor Follower — Record
cls
echo.
echo   ========================================
echo    Cursor Follower — Screen Recorder
echo   ========================================
echo.
echo   A setup window will ask for:
echo     1. Video quality   (Low / HD / 2K)
echo     2. Microphone
echo     3. Frame size      (16:9 / 9:16 / custom)
echo.
echo   The red border follows your cursor.
echo   Everything inside it is recorded, with voice.
echo   Press Esc to stop and save (folder: recordings).
echo.

where py >nul 2>&1 && (
  py -3 -c "import mss,numpy,imageio_ffmpeg" 2>nul || py -3 -m pip install -r "%~dp0requirements.txt"
  py -3 "%~dp0cursor_border.py"
  goto after
)
python -c "import mss,numpy,imageio_ffmpeg" 2>nul || python -m pip install -r "%~dp0requirements.txt"
python "%~dp0cursor_border.py"

:after
echo.
if errorlevel 1 (
  echo   Something went wrong. Is Python installed and on PATH?
  echo.
)
pause
endlocal

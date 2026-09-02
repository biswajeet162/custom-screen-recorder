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
echo     1. Video quality   (Low / HD / 2K / 4K)
echo     2. Frame rate      (24 / 30 / 60 fps)
echo     3. Microphone
echo     4. Frame size      (16:9 / 9:16 / custom)
echo.
echo   The cyan box stays on screen. Zoom toward a corner and those edges stick.
echo   Pick 16:9, 9:16, or custom to see that size immediately.
echo   4K files stay 3840x2160 even if the viewfinder is smaller on this screen.
echo   Press Ctrl+Caps Lock to park the border (stops following).
echo   Hold Ctrl+Shift and Right arrow to zoom in smoothly. Near a corner, those edges stay on screen.
echo   Hold Ctrl+Shift and Left arrow to zoom out. Ratio stays locked.
echo   Red rec dot sits in the bottom-left corner with the timer below it.
echo   Hover the dot for Start, Stop, Refresh, and Exit.
echo   Start waits 3-2-1. Stop is immediate. Exit saves the video and quits.
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

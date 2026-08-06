@echo off
setlocal EnableExtensions
cd /d "%~dp0"

title Cursor Border Overlay
cls
echo.
echo   ========================================
echo    Cursor Border Overlay
echo   ========================================
echo.
echo   Option 1  -  16:9   (widescreen)
echo   Option 2  -  9:16   (vertical / portrait)
echo.
echo   The cyan border follows your cursor.
echo   Press Esc in the overlay to quit.
echo.
set "CHOICE="
set /p "CHOICE=  Enter 1 or 2, then press Enter: "

if /i "%CHOICE%"=="1" goto run_169
if /i "%CHOICE%"=="2" goto run_916

echo.
echo   Invalid choice. Please run again and pick 1 or 2.
echo.
pause
exit /b 1

:run_169
echo.
echo   Starting 16:9 border...
where py >nul 2>&1 && (
  py -3 "%~dp0cursor_border.py" 16:9
  goto after
)
python "%~dp0cursor_border.py" 16:9
goto after

:run_916
echo.
echo   Starting 9:16 border...
where py >nul 2>&1 && (
  py -3 "%~dp0cursor_border.py" 9:16
  goto after
)
python "%~dp0cursor_border.py" 9:16

:after
if errorlevel 1 (
  echo.
  echo   Something went wrong. Is Python installed and on PATH?
  echo.
  pause
)
endlocal

@echo off
rem Double-click to run now. The report opens in the browser when done.
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
python -u daytrade.py
if errorlevel 1 echo. & echo *** Finished with problems, see messages above ***
start "" "%~dp0docs\index.html"
echo.
pause

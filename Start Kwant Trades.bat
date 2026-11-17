@echo off
rem Starts the Kwant Trades dashboard on this PC only and opens it in your browser.
cd /d "%~dp0"
title Kwant Trades (close this window to stop)
python -m kwanttrades web
echo.
echo Kwant Trades has stopped. Press any key to close this window.
pause >nul

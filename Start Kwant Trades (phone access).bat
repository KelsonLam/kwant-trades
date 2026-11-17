@echo off
rem Starts the Kwant Trades dashboard so your phone can open it through Tailscale.
rem Needs: Tailscale connected on this PC, and KWANT_WEB_PASSWORD set in .env.
cd /d "%~dp0"
title Kwant Trades - phone access (close this window to stop)
python -m kwanttrades web --tailscale --no-browser
echo.
echo Kwant Trades has stopped. Press any key to close this window.
pause >nul

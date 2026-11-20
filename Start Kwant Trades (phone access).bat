@echo off
rem Starts the Kwant Trades dashboard so your phone can open it through Tailscale.
rem Needs: Tailscale connected on this PC, and KWANT_WEB_PASSWORD set in .env.
rem If the "Kwant Trades" scheduled task is set up, it already does this at logon; this file is for manual starts.
cd /d "%~dp0"
title Kwant Trades - phone access (close this window to stop)
python -m kwanttrades web --tailscale --wait-tailscale 180 --no-browser --keep-awake --start-session auto
echo.
echo Kwant Trades has stopped. Press any key to close this window.
pause >nul

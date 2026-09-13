@echo off
title Smart Attendance System - Debug Mode
echo ==========================================
echo Starting Smart Attendance System...
echo Press Ctrl+C to stop the system.
echo ==========================================

:loop
python main.py
echo.
echo ==========================================
echo System exited or crashed.
echo See crash_log.txt for details if generated.
echo Restarting in 5 seconds...
echo Press Ctrl+C to stop.
echo ==========================================
timeout /t 5
goto loop
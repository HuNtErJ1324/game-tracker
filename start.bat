@echo off
rem Game Tracker launcher. Usage:
rem   start.bat                     run everything on this PC and open the dashboard
rem   start.bat agent --server URL --token TOKEN    report to a home server instead
setlocal
rem The venv lives in a short path: Python can't load compiled modules from very long paths.
set "VENV=%LOCALAPPDATA%\GameTracker\venv"
if not exist "%VENV%\Scripts\python.exe" (
  echo Setting up Python environment in %VENV% ...
  py -3 -m venv "%VENV%" 2>nul || python -m venv "%VENV%" || (echo Python 3.10+ is required. & pause & exit /b 1)
)
"%VENV%\Scripts\python.exe" -m pip install -q --disable-pip-version-check -r "%~dp0requirements.txt" || (pause & exit /b 1)
cd /d "%~dp0"
"%VENV%\Scripts\python.exe" -m gametracker %*

@echo off
cd /d "%~dp0"
start "" venv\Scripts\python.exe dashboard.py
timeout /t 2 /nobreak >nul
start "" http://localhost:5000

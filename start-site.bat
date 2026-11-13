@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Запускаю Korosten Railways...
python server.py
if errorlevel 1 pause

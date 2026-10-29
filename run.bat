@echo off
rem Starts spylead in this console, from the venv, with the config.toml next to this file. Ctrl+C stops it.
cd /d "%~dp0"
".venv\Scripts\python.exe" -m spylead --config config.toml
rem A config error, or another spylead already running, must stay on screen until read.
if errorlevel 1 pause

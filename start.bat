@echo off
cd /d "%~dp0"
if exist venv\Scripts\activate.bat call venv\Scripts\activate.bat
start "" http://127.0.0.1:8820
python app.py

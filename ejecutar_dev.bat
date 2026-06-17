@echo off
cd /d C:\SIGEMEP_APP_DEV
call venv\Scripts\activate
uvicorn app:app --host 0.0.0.0 --port 8001 --reload
pause

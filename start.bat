@echo off
cd /d "%~dp0"

rem Запуск из исходников. Берём первое найденное окружение:
rem локальный python\, затем .venv, затем python из PATH.
set "PY="
if exist "python\python.exe" set "PY=python\python.exe"
if not defined PY if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY set "PY=python"

"%PY%" compacttui.py %*
if errorlevel 1 pause

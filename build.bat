@echo off
cd /d "%~dp0"

rem Собрать один CompactTui.exe. Берём первое найденное окружение с PyInstaller:
rem локальный python\, затем .venv, затем python из PATH.
set "PY="
if exist "python\python.exe" set "PY=python\python.exe"
if not defined PY if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY set "PY=python"

echo Building with %PY%
"%PY%" -m PyInstaller --onefile --noconfirm compacttui.py
if errorlevel 1 goto fail

copy /y "dist\CompactTui.exe" "CompactTui.exe" >nul
if errorlevel 1 goto fail

echo Built CompactTui.exe
pause
exit /b 0

:fail
echo Build failed
pause
exit /b 1

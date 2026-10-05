@echo off
setlocal EnableExtensions
cd /d "%~dp0"
rem The tool carries its own Python runtime (folder "python"), nothing needs to be installed.
if exist "python\pythonw.exe" goto run
if not exist "runtime.zip" goto noruntime
echo Unpacking the built-in runtime (first run only, a few seconds)...
tar -xf runtime.zip >nul 2>nul
if not exist "python\pythonw.exe" powershell -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -Force -LiteralPath 'runtime.zip' -DestinationPath '.'" >nul 2>nul
if not exist "python\pythonw.exe" goto noruntime
del /q runtime.zip >nul 2>nul

:run
start "" "%~dp0python\pythonw.exe" "%~dp0cn_gui.py"
exit /b 0

:noruntime
echo [ERROR] The built-in runtime is missing or could not be unpacked.
pause
exit /b 1

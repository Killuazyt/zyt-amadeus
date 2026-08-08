@echo off
setlocal

set "AMADEUS_ROOT=%~dp0"
set "AMADEUS_DESKTOP=%AMADEUS_ROOT%desktop"
set "AMADEUS_PYTHONW=%AMADEUS_DESKTOP%\.venv\Scripts\pythonw.exe"

if not exist "%AMADEUS_PYTHONW%" (
    echo Amadeus virtual environment was not found.
    echo Expected: "%AMADEUS_PYTHONW%"
    echo Run desktop\scripts\bootstrap.ps1 first.
    pause
    exit /b 1
)

start "" /D "%AMADEUS_DESKTOP%" "%AMADEUS_PYTHONW%" -m amadeus_desktop
exit /b 0

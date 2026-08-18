@echo off
rem Build a standalone smsblast.exe on Windows.
rem Messages are in ASCII on purpose: the Windows console reads .bat files
rem in the OEM code page, and Cyrillic here turns into mojibake.
chcp 65001 >nul 2>&1
setlocal

cd /d "%~dp0"

set "PROJECT=%~1"
if not defined PROJECT set "PROJECT=%~dp0..\рассылка"

rem Sources may already be copied into src\ by build.sh on a Mac.
if exist "src\app.py" (
  echo Using sources already present in src\
  goto :build
)

if not exist "%PROJECT%\app.py" (
  echo.
  echo Cannot find the project.
  echo Usage: build-windows.bat "C:\path\to\project-folder"
  echo Or copy the project sources into the src\ subfolder next to this script.
  exit /b 1
)

echo Copying sources from "%PROJECT%" ...
if exist src rmdir /s /q src
mkdir src
copy /y "%PROJECT%\app.py" src\ >nul
xcopy /e /i /y /q "%PROJECT%\smsblast"  src\smsblast\  >nul
xcopy /e /i /y /q "%PROJECT%\templates" src\templates\ >nul
xcopy /e /i /y /q "%PROJECT%\static"    src\static\    >nul

rem The database holds the gateway password - it must never be bundled.
del /s /q src\*.db src\*.db-wal src\*.db-shm src\.env >nul 2>&1
for /d /r src %%d in (__pycache__) do @if exist "%%d" rmdir /s /q "%%d"

:build
if not exist ".venv-build\Scripts\pyinstaller.exe" (
  echo Preparing build environment ...
  python -m venv .venv-build || (echo Python 3 is required in PATH & exit /b 1)
  .venv-build\Scripts\python -m pip install --quiet --upgrade pip
  .venv-build\Scripts\pip install --quiet flask requests openpyxl python-dotenv pyinstaller
)

echo Building ...
.venv-build\Scripts\pyinstaller --noconfirm --clean smsblast.spec
if errorlevel 1 exit /b 1

echo.
if exist "dist\smsblast.exe" (
  echo Done: %~dp0dist\smsblast.exe
  echo The app prints its data folder path on startup (under %%APPDATA%%).
  echo.
  echo Note: adb is required for the USB mode. Install Android Platform Tools
  echo and make sure adb.exe is in PATH.
) else (
  echo Build did not produce dist\smsblast.exe - see the output above.
  exit /b 1
)

endlocal

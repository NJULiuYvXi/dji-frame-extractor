@echo off
REM ===================================================================
REM  One-click Windows build for extract_frames_with_gps_hwaccel.
REM
REM  Prerequisites on the Windows machine:
REM    - Python 3.10 or newer, installed with "Add Python to PATH" ticked.
REM      Get it from https://www.python.org/downloads/windows/
REM      For a 64-bit exe  -> 64-bit Python (default download).
REM      For a 32-bit exe  -> 32-bit ("x86") Python installer.
REM    - Internet access (first build only, to fetch ffmpeg/exiftool).
REM
REM  What it does:
REM    1. Verifies `python` is on PATH.
REM    2. Installs PyInstaller into the current Python if missing.
REM    3. Runs fetch_deps.ps1 to download ffmpeg/ffprobe/exiftool into bin\
REM       (skipped if bin\ffmpeg.exe already exists).
REM    4. Cleans previous build\ and dist\ folders.
REM    5. Runs PyInstaller against build.spec.
REM
REM  Output:
REM    dist\extract_frames_hwaccel.exe   (single-file, ~200-300 MB)
REM ===================================================================

setlocal ENABLEEXTENSIONS
cd /d "%~dp0"

echo.
echo === [1/4] Checking Python ===
where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] python.exe is not on PATH.
    echo         Install Python 3.10+ from https://www.python.org/downloads/windows/
    echo         and re-run this script from a fresh Command Prompt.
    exit /b 1
)
python --version
python -c "import struct, sys; print('Python arch:', struct.calcsize('P')*8, 'bit'); print('Executable:', sys.executable)"

echo.
echo === [2/4] Ensuring PyInstaller is installed ===
python -c "import PyInstaller" 1>nul 2>nul
if errorlevel 1 (
    echo PyInstaller not found; installing into this Python...
    python -m pip install --upgrade pip
    if errorlevel 1 exit /b 1
    python -m pip install pyinstaller
    if errorlevel 1 exit /b 1
) else (
    python -c "import PyInstaller; print('PyInstaller', PyInstaller.__version__)"
)

echo.
echo === [3/4] Ensuring ffmpeg/ffprobe/exiftool are present in bin\ ===
if exist "bin\ffmpeg.exe" if exist "bin\ffprobe.exe" if exist "bin\exiftool.exe" (
    echo bin\ already populated; skipping fetch.
    goto build
)
echo Fetching dependencies via PowerShell...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0fetch_deps.ps1"
if errorlevel 1 (
    echo [ERROR] fetch_deps.ps1 failed. Check your internet connection, or
    echo         download ffmpeg.exe, ffprobe.exe, exiftool.exe manually into
    echo         the bin\ folder and re-run this script.
    exit /b 1
)

:build
echo.
echo === [4/4] Running PyInstaller ===
if exist "build" rmdir /s /q "build"
if exist "dist"  rmdir /s /q "dist"

python -m PyInstaller build.spec --clean --noconfirm
if errorlevel 1 (
    echo [ERROR] PyInstaller build failed. Scroll up for details.
    exit /b 1
)

echo.
echo ===================================================================
echo  Build OK.
echo  Output: %CD%\dist\extract_frames_hwaccel.exe
echo ===================================================================
dir /b dist
exit /b 0

@echo off
chcp 65001 >nul
title Berlin Termin Bot (Web)
cd /d "%~dp0"

echo ============================================
echo   Berlin Termin Bot (Web) baslatiliyor...
echo ============================================
echo.

if not exist "app.py" (
    echo [HATA] app.py bu klasorde yok!
    echo Bu bat dosyasi ile app.py AYNI klasorde olmali.
    echo Su anki klasor: %CD%
    echo.
    pause
    exit /b 1
)

REM ---- Python'u bul ----
set "PY="
if exist "C:\Users\Aysel\AppData\Roaming\kimi-desktop\daimon-bundle\runtime\python\cpython-3.12\python.exe" (
    set "PY=C:\Users\Aysel\AppData\Roaming\kimi-desktop\daimon-bundle\runtime\python\cpython-3.12\python.exe"
)
if exist "C:\Users\Aysel\AppData\Roaming\kimi-desktop\daimon-share\daimon\runtime\python\.venv\Scripts\python.exe" (
    set "PY=C:\Users\Aysel\AppData\Roaming\kimi-desktop\daimon-share\daimon\runtime\python\.venv\Scripts\python.exe"
)
if not defined PY (
    where python >nul 2>nul
    if %errorlevel%==0 set "PY=python"
)
if not defined PY (
    echo [HATA] Python bulunamadi!
    echo Lutfen https://www.python.org/downloads/ adresinden Python kurun
    echo (kurulumda "Add Python to PATH" secenegini isaretleyin).
    echo.
    pause
    exit /b 1
)

echo [BILGI] Python: %PY%
echo [BILGI] Klasor: %CD%
echo.

REM ---- Bagimliliklari kur (ilk calistirmada) ----
"%PY%" -c "import fastapi, uvicorn, playwright" 2>nul
if errorlevel 1 (
    echo [UYARI] Gerekli paketler eksik, simdi kuruluyor...
    "%PY%" -m pip install --upgrade pip
    "%PY%" -m pip install -r requirements.txt
    echo.
)

REM ---- Playwright Chromium tarayicisini kur ----
"%PY%" -m playwright install chromium

REM ---- Masaustunde tarayiciyi GORUNUR calistir (CAPTCHA'yi elle cozebilmek icin) ----
set "HEADLESS=0"

echo.
echo [BILGI] Arayuz birkac saniye icinde tarayicida acilacak:
echo         http://localhost:8000
echo.

REM Sunucu ayaga kalkinca (4 sn sonra) tarayiciyi ac
start "" cmd /c "timeout /t 4 >nul & start "" http://localhost:8000"

REM Sunucuyu baslat (bu pencere acik kaldigi surece bot calisir)
"%PY%" app.py

echo.
echo ============================================
echo   Sunucu kapandi.
echo ============================================
pause

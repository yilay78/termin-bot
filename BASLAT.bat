@echo off
chcp 65001 >nul
title Berlin Termin Bot (Web)
cd /d "%~dp0"
setlocal enabledelayedexpansion

echo ============================================
echo   Berlin Termin Bot (Web) baslatiliyor...
echo ============================================
echo.

if not exist "app.py" goto no_app

REM ---- Python'u bul ----
set "PY="
if exist "C:\Users\Aysel\AppData\Roaming\kimi-desktop\daimon-bundle\runtime\python\cpython-3.12\python.exe" set "PY=C:\Users\Aysel\AppData\Roaming\kimi-desktop\daimon-bundle\runtime\python\cpython-3.12\python.exe"
if exist "C:\Users\Aysel\AppData\Roaming\kimi-desktop\daimon-share\daimon\runtime\python\.venv\Scripts\python.exe" set "PY=C:\Users\Aysel\AppData\Roaming\kimi-desktop\daimon-share\daimon\runtime\python\.venv\Scripts\python.exe"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY where py >nul 2>nul && set "PY=py"
if not defined PY goto no_python

echo [BILGI] Python: !PY!
echo [BILGI] Klasor : %CD%
echo.

REM ---- Gerekli paketleri kur (ilk calistirmada) ----
"!PY!" -c "import fastapi, uvicorn, playwright" 2>nul
if errorlevel 1 (
    echo [BILGI] Gerekli paketler kuruluyor, lutfen bekleyin...
    "!PY!" -m pip install --upgrade pip
    "!PY!" -m pip install -r requirements.txt
    if errorlevel 1 goto pip_error
    echo.
)

REM ---- Playwright Chromium tarayicisini kur (ilk sefer ~150 MB) ----
echo [BILGI] Tarayici bileseni kontrol ediliyor...
"!PY!" -m playwright install chromium

REM ---- Masaustunde tarayiciyi GORUNUR calistir (CAPTCHA icin) ----
set "HEADLESS=0"

echo.
echo [BILGI] Arayuz birkac saniye icinde tarayicida acilacak:
echo         http://localhost:8000
echo.

REM Sunucu ayaga kalkinca (~5 sn) tarayiciyi ac
start "" cmd /c "ping -n 6 127.0.0.1 >nul & start http://localhost:8000"

REM Sunucuyu baslat (bu pencere acik kaldigi surece bot calisir)
"!PY!" app.py

echo.
echo ============================================
echo   Sunucu kapandi.
echo ============================================
pause
goto son

:no_app
echo [HATA] app.py bu klasorde yok!
echo Bu bat dosyasi ile app.py AYNI klasorde olmali.
echo Su anki klasor: %CD%
echo.
pause
goto son

:no_python
echo [HATA] Python bulunamadi!
echo Lutfen https://www.python.org/downloads/ adresinden Python kurun.
echo Kurulum sirasinda "Add Python to PATH" kutusunu MUTLAKA isaretleyin.
echo.
pause
goto son

:pip_error
echo [HATA] Paketler kurulamadi. Internet baglantinizi kontrol edin.
echo.
pause
goto son

:son
endlocal

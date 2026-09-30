@echo off
setlocal
REM Dong goi PingDiagnose thanh exe (chay tren Windows, can Python 3.10+)
cd /d "%~dp0"
if not exist .venv (
    python -m venv .venv || goto :err
)
call .venv\Scripts\activate.bat || goto :err
python -m pip install --upgrade pip >nul
python -m pip install -r requirements-build.txt || goto :err
python -m pytest -q tests || goto :err
pyinstaller --noconfirm --clean PingDiagnose.spec || goto :err
copy /Y install.bat dist\PingDiagnose\ >nul
copy /Y uninstall.bat dist\PingDiagnose\ >nul
copy /Y README.md dist\PingDiagnose\ >nul
powershell -NoProfile -Command "Compress-Archive -Force -Path dist\PingDiagnose -DestinationPath dist\PingDiagnose-win64.zip" || goto :err
echo.
echo Xong: dist\PingDiagnose\  va  dist\PingDiagnose-win64.zip
exit /b 0
:err
echo Build that bai.
exit /b 1

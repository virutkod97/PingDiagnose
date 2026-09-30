@echo off
setlocal EnableExtensions
REM ================================================================
REM  PingDiagnose - cai dat Windows Service
REM  Cach dung:  install.bat [PORT]     (mac dinh PORT = 8080)
REM ================================================================
set "SVC=PingDiagnose"
set "PORT=%~1"
if "%PORT%"=="" set "PORT=8080"
set "SRC=%~dp0"
if "%SRC:~-1%"=="\" set "SRC=%SRC:~0,-1%"
set "DEST=%ProgramFiles%\PingDiagnose"

REM --- Yeu cau quyen Administrator -------------------------------
net session >nul 2>&1
if errorlevel 1 (
    echo Dang yeu cau quyen Administrator...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '%PORT%' -Verb RunAs"
    exit /b
)

if not exist "%SRC%\PingDiagnose.exe" (
    echo [LOI] Khong tim thay PingDiagnose.exe trong "%SRC%"
    pause
    exit /b 1
)

echo.
echo === Cai dat PingDiagnose vao "%DEST%" (cong %PORT%) ===
echo.

REM --- Go service cu neu da cai (nang cap) ------------------------
sc query "%SVC%" >nul 2>&1
if not errorlevel 1 (
    echo Phat hien ban cai cu - dang dung service...
    sc stop "%SVC%" >nul 2>&1
    call :waitstop
    sc delete "%SVC%" >nul 2>&1
    timeout /t 2 /nobreak >nul
)

REM --- Sao chep file ---------------------------------------------
if /I not "%SRC%"=="%DEST%" (
    if not exist "%DEST%" mkdir "%DEST%"
    robocopy "%SRC%" "%DEST%" /E /NFL /NDL /NJH /NJS /NP /R:3 /W:2 >nul
    if errorlevel 8 (
        echo [LOI] Sao chep file that bai.
        pause
        exit /b 1
    )
)

REM --- Cau hinh cong web -----------------------------------------
"%DEST%\PingDiagnose.exe" config --port %PORT%
if errorlevel 1 (
    echo [LOI] Khong ghi duoc cau hinh.
    pause
    exit /b 1
)

REM --- Tao service -----------------------------------------------
sc create "%SVC%" binPath= "\"%DEST%\PingDiagnose.exe\"" start= delayed-auto DisplayName= "PingDiagnose - Giam sat ket noi IP" >nul
if errorlevel 1 (
    echo [LOI] Khong tao duoc service.
    pause
    exit /b 1
)
sc description "%SVC%" "Dinh ky ping cac dia chi IP, canh bao mat ket noi va thong ke qua giao dien web (cong %PORT%)." >nul
sc failure "%SVC%" reset= 86400 actions= restart/5000/restart/10000/restart/30000 >nul

REM --- Mo firewall -----------------------------------------------
netsh advfirewall firewall delete rule name="PingDiagnose Web" >nul 2>&1
netsh advfirewall firewall add rule name="PingDiagnose Web" dir=in action=allow protocol=TCP localport=%PORT% >nul

REM --- Khoi dong -------------------------------------------------
sc start "%SVC%" >nul
timeout /t 3 /nobreak >nul
sc query "%SVC%" | find "RUNNING" >nul
if errorlevel 1 (
    echo [CANH BAO] Service chua o trang thai RUNNING. Xem log tai:
    echo     %ProgramData%\PingDiagnose\logs\pingdiagnose.log
) else (
    echo Service PingDiagnose dang chay.
)

echo.
echo  Giao dien web : http://localhost:%PORT%
echo  Tai khoan     : admin / admin  (bat buoc doi mat khau khi dang nhap lan dau)
echo  Du lieu       : %ProgramData%\PingDiagnose
echo.
start "" "http://localhost:%PORT%"
pause
exit /b 0

:waitstop
for /L %%i in (1,1,15) do (
    sc query "%SVC%" | find "STOPPED" >nul && exit /b 0
    timeout /t 1 /nobreak >nul
)
exit /b 0

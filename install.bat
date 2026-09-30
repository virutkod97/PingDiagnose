@echo off
setlocal EnableExtensions
set "SVC=PingDiagnose"
set "PORT=%~1"
if "%PORT%"=="" set "PORT=8443"
set "SRC=%~dp0"
if "%SRC:~-1%"=="\" set "SRC=%SRC:~0,-1%"
set "DEST=%ProgramFiles%\PingDiagnose"
set "DATA=%ProgramData%\PingDiagnose"

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
echo === Cai dat PingDiagnose vao "%DEST%" - cong %PORT% ===
echo.

sc query "%SVC%" >nul 2>&1
if not errorlevel 1 (
    echo Phat hien ban cai cu - dang dung service...
    sc stop "%SVC%" >nul 2>&1
    call :waitstop
    sc delete "%SVC%" >nul 2>&1
    timeout /t 2 /nobreak >nul
)

if /I not "%SRC%"=="%DEST%" (
    if not exist "%DEST%" mkdir "%DEST%"
    robocopy "%SRC%" "%DEST%" /E /NFL /NDL /NJH /NJS /NP /R:3 /W:2 >nul
    if errorlevel 8 (
        echo [LOI] Sao chep file that bai.
        pause
        exit /b 1
    )
)

"%DEST%\PingDiagnose.exe" config --port %PORT% --https on >nul
if errorlevel 1 (
    echo [LOI] Khong ghi duoc cau hinh.
    pause
    exit /b 1
)

echo Tao chung chi HTTPS...
"%DEST%\PingDiagnose.exe" cert >nul
if exist "%DATA%\ca.crt" (
    certutil -f -addstore Root "%DATA%\ca.crt" >nul && echo Da cai chung chi CA len may chu.
)

sc create "%SVC%" binPath= "\"%DEST%\PingDiagnose.exe\"" start= delayed-auto DisplayName= "PingDiagnose - Giam sat ket noi IP" >nul
if errorlevel 1 (
    echo [LOI] Khong tao duoc service.
    pause
    exit /b 1
)
sc description "%SVC%" "Dinh ky ping cac dia chi IP, canh bao mat ket noi va thong ke qua giao dien web - cong %PORT%." >nul
sc failure "%SVC%" reset= 86400 actions= restart/5000/restart/10000/restart/30000 >nul

netsh advfirewall firewall delete rule name="PingDiagnose Web" >nul 2>&1
netsh advfirewall firewall add rule name="PingDiagnose Web" dir=in action=allow protocol=TCP localport=%PORT% >nul

sc start "%SVC%" >nul
timeout /t 3 /nobreak >nul
sc query "%SVC%" | find "RUNNING" >nul
if errorlevel 1 (
    echo [CANH BAO] Service chua chay. Xem log: %DATA%\logs\pingdiagnose.log
) else (
    echo Service PingDiagnose dang chay.
)

echo.
echo  Giao dien web : https://%COMPUTERNAME%:%PORT%   hoac   https://IP-may-chu:%PORT%
echo  Tai khoan     : admin / admin - bat buoc doi mat khau lan dau
echo  May tram      : vao trang dang nhap, bam "Cai chung chi cho may nay" de het canh bao chung chi
echo  Du lieu       : %DATA%
echo.
start "" "https://localhost:%PORT%"
pause
exit /b 0

:waitstop
for /L %%i in (1,1,15) do (
    sc query "%SVC%" | find "STOPPED" >nul && exit /b 0
    timeout /t 1 /nobreak >nul
)
exit /b 0

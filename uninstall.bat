@echo off
setlocal EnableExtensions
set "SVC=PingDiagnose"
set "DEST=%ProgramFiles%\PingDiagnose"
set "DATA=%ProgramData%\PingDiagnose"

net session >nul 2>&1
if errorlevel 1 (
    echo Dang yeu cau quyen Administrator...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

echo.
echo === Go cai dat PingDiagnose ===
echo.

sc query "%SVC%" >nul 2>&1
if not errorlevel 1 (
    echo Dang dung service...
    sc stop "%SVC%" >nul 2>&1
    call :waitstop
    sc delete "%SVC%" >nul
    echo Da xoa service.
) else (
    echo Service chua duoc cai dat.
)
taskkill /F /IM PingDiagnose.exe >nul 2>&1
netsh advfirewall firewall delete rule name="PingDiagnose Web" >nul 2>&1
certutil -delstore Root "PingDiagnose CA (%COMPUTERNAME%)" >nul 2>&1

echo.
choice /C YN /N /M "Xoa ca du lieu - danh sach IP, lich su, tai khoan, chung chi - tai %DATA% ? [Y/N]: "
if errorlevel 2 goto keepdata
rmdir /S /Q "%DATA%" 2>nul
echo Da xoa du lieu.
goto done
:keepdata
echo Giu lai du lieu tai %DATA%.

:done
echo.
echo Go cai dat hoan tat.
pause
if exist "%DEST%" start "" /MIN cmd /c "timeout /t 2 /nobreak >nul & rmdir /S /Q "%DEST%""
exit /b 0

:waitstop
for /L %%i in (1,1,15) do (
    sc query "%SVC%" | find "STOPPED" >nul && exit /b 0
    timeout /t 1 /nobreak >nul
)
exit /b 0

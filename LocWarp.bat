@echo off
setlocal EnableExtensions
set "LOCWARP_ACTION=%~1"
set "PYTHON_EXE=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"

if /I "%LOCWARP_ACTION%" == "help" goto :USAGE
if /I "%LOCWARP_ACTION%" == "/?" goto :USAGE
if /I "%LOCWARP_ACTION%" == "-?" goto :USAGE

:: Check admin. Both repair modes are intentionally elevated because PnP and
:: the Wintun driver store are protected Windows resources.
net session >nul 2>&1
if not errorlevel 1 goto :DISPATCH

:: Elevate using VBScript and preserve the optional action (repair/help).
echo Set UAC = CreateObject^("Shell.Application"^) > "%temp%\locwarp_elevate.vbs"
echo UAC.ShellExecute "%~f0", "%~1", "%~dp0", "runas", 1 >> "%temp%\locwarp_elevate.vbs"
cscript //nologo "%temp%\locwarp_elevate.vbs"
del "%temp%\locwarp_elevate.vbs"
exit /b

:DISPATCH
if /I "%LOCWARP_ACTION%" == "repair" goto :REPAIR
if /I "%LOCWARP_ACTION%" == "repair-driver" goto :REPAIR_DRIVER
if /I "%LOCWARP_ACTION%" == "userspace" goto :RUN_USERSPACE
if /I "%LOCWARP_ACTION%" == "kernel" goto :RUN_KERNEL
goto :RUN

:USAGE
echo.
echo LocWarp usage:
echo   LocWarp.bat                  start isolated multi-iPhone tunnels
echo   LocWarp.bat repair           repair WinTun/PnP without reboot
echo   LocWarp.bat repair-driver    reset the WinTun driver store
echo   LocWarp.bat userspace        start isolated multi-iPhone tunnels
echo   LocWarp.bat kernel           use the Windows WinTun driver
echo.
echo Run repair-driver only after closing LocWarp tunnel and other VPN/WireGuard
echo applications that use WinTun. It refuses to run when WinTun is active.
echo Normal startup avoids WinTun with a separate tunnel process for each iPhone.
echo Neither repair mode reboots Windows automatically.
exit /b 0

:REPAIR
cd /d "%~dp0"
echo.
echo [LocWarp] Running WinTun/PnP repair without reboot...
echo [LocWarp] Physical adapters and the default route are not modified.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\repair_wintun.ps1" -Mode safe
set "REPAIR_EXIT=%ERRORLEVEL%"
if "%REPAIR_EXIT%" == "3" echo [LocWarp] Repair skipped because a WinTun tunnel is active.
if "%REPAIR_EXIT%" == "2" echo [LocWarp] Repair finished with remaining PnP problems.
if "%REPAIR_EXIT%" == "4" echo [LocWarp] Repair needs a Windows restart to finish PnP class configuration; no restart was performed.
if "%REPAIR_EXIT%" == "1" echo [LocWarp] Repair failed before completion.
if "%REPAIR_EXIT%" == "0" echo [LocWarp] Repair completed. Restart LocWarp to test the iPhone tunnel.
pause
exit /b %REPAIR_EXIT%

:REPAIR_DRIVER
cd /d "%~dp0"
echo.
echo [LocWarp] Running the WinTun driver store reset...
echo [LocWarp] This mode requires all WinTun interfaces to be inactive.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\repair_wintun.ps1" -Mode driver -PythonExe "%PYTHON_EXE%"
set "REPAIR_EXIT=%ERRORLEVEL%"
if "%REPAIR_EXIT%" == "3" echo [LocWarp] Driver reset skipped because a WinTun tunnel is active.
if "%REPAIR_EXIT%" == "4" echo [LocWarp] Driver reset completed, but Windows still reports Code 56/4319; a restart is required and was not performed.
if "%REPAIR_EXIT%" == "1" echo [LocWarp] Driver store reset failed; no reboot was performed.
if "%REPAIR_EXIT%" == "0" echo [LocWarp] Driver store reset. The next tunnel creation will reinstall WinTun.
pause
exit /b %REPAIR_EXIT%

:RUN_USERSPACE
set "LOCWARP_TUNNEL_TRANSPORT=userspace-process"
goto :RUN

:RUN_KERNEL
set "LOCWARP_TUNNEL_TRANSPORT=kernel"
set "LOCWARP_USE_USERSPACE_TUNNEL="
goto :RUN

:RUN
cd /d "%~dp0"
if not defined LOCWARP_TUNNEL_TRANSPORT set "LOCWARP_TUNNEL_TRANSPORT=userspace-process"
echo [LocWarp] Tunnel transport: %LOCWARP_TUNNEL_TRANSPORT%
:: Use the full path to avoid the Windows Store python stub
"%PYTHON_EXE%" start.py
pause

[CmdletBinding()]
param(
    [ValidateSet('safe', 'driver')]
    [string]$Mode = 'safe',
    [string]$PythonExe = ''
)

$ErrorActionPreference = 'Stop'
$pnputil = Join-Path $env:windir 'System32\pnputil.exe'

function Get-ActiveWintunAdapter {
    @(Get-NetAdapter -IncludeHidden -ErrorAction SilentlyContinue | Where-Object {
        $_.Status -eq 'Up' -and (
            $_.Name -match '(?i)wintun' -or
            $_.InterfaceDescription -match '(?i)wintun'
        )
    })
}

function Get-WintunPnpDevice {
    @(Get-CimInstance -ClassName Win32_PnPEntity -ErrorAction Stop | Where-Object {
        $_.Name -match '(?i)wintun' -or $_.Service -eq 'wintun'
    })
}

function Test-LocWarpTunnelActive {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 -Uri 'http://127.0.0.1:8777/api/device/wifi/tunnel/status'
        $payload = $response.Content | ConvertFrom-Json
        return $payload.running -eq $true -or @($payload.tunnels).Count -gt 0
    }
    catch {
        return $false
    }
}

function Test-PnpFailure {
    param([object]$Device)

    return $Device.Status -ne 'OK' -or ([int]$Device.ConfigManagerErrorCode) -ne 0
}

function Invoke-PnpUtil {
    param([string[]]$Arguments)

    if (-not (Test-Path -LiteralPath $pnputil)) {
        throw "找不到 pnputil.exe：$pnputil"
    }

    & $pnputil @Arguments
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "pnputil $($Arguments -join ' ') 回傳 exit $LASTEXITCODE"
        return $false
    }

    return $true
}

function Assert-NoActiveWintun {
    $active = @(Get-ActiveWintunAdapter)
    $locWarpActive = Test-LocWarpTunnelActive
    if ($active.Count -gt 0 -or $locWarpActive) {
        $names = $active | ForEach-Object { $_.Name }
        $detail = if ($names) { "介面：$($names -join ', ')" } else { 'LocWarp API 回報 tunnel 正在執行' }
        Write-Warning "偵測到啟用中的 WinTun（$detail）；為避免中斷 tunnel/VPN，未修改裝置或 driver。"
        exit 3
    }
}

Write-Host "[LocWarp] WinTun repair mode: $Mode"
Write-Host '[LocWarp] 實體網卡與預設路由不在此流程的操作範圍。'
Assert-NoActiveWintun

if ($Mode -eq 'safe') {
    $services = @('DeviceInstall', 'DsmSvc', 'NetSetupSvc')
    foreach ($name in $services) {
        try {
            $service = Get-Service -Name $name -ErrorAction Stop
            if ($service.Status -eq 'Stopped') {
                Start-Service -Name $name -ErrorAction Stop
                $service = Get-Service -Name $name -ErrorAction Stop
                Write-Host "  service ${name}: started ($($service.Status))"
            }
            else {
                Write-Host "  service ${name}: $($service.Status) (left unchanged)"
            }
        }
        catch {
            Write-Warning "  service ${name}: $($_.Exception.Message)"
        }
    }

    Write-Host '  PnP scan:'
    [void](Invoke-PnpUtil @('/scan-devices'))

    $failed = @(Get-WintunPnpDevice | Where-Object { Test-PnpFailure $_ })
    foreach ($device in $failed) {
        Write-Host "  restart WinTun device: $($device.PNPDeviceID)"
        [void](Invoke-PnpUtil @('/restart-device', $device.PNPDeviceID))
    }

    Start-Sleep -Milliseconds 800
    Write-Host '  PnP rescan:'
    [void](Invoke-PnpUtil @('/scan-devices'))

    $remaining = @(Get-WintunPnpDevice | Where-Object { Test-PnpFailure $_ })
    if ($remaining.Count -gt 0) {
        Write-Warning "WinTun 仍有 $($remaining.Count) 個 PnP 問題；確認沒有其他 VPN/WireGuard 使用它後，再執行 repair-driver。"
        exit 2
    }

    Write-Host 'WinTun/PnP repair completed.'
    exit 0
}

if ([string]::IsNullOrWhiteSpace($PythonExe)) {
    $PythonExe = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe'
}

if (-not (Test-Path -LiteralPath $PythonExe)) {
    throw "找不到 Python：$PythonExe"
}

$failed = @(Get-WintunPnpDevice | Where-Object { Test-PnpFailure $_ })
$removeFailed = $false
foreach ($device in $failed) {
    Write-Host "  remove failed WinTun device: $($device.PNPDeviceID)"
    if (-not (Invoke-PnpUtil @('/remove-device', $device.PNPDeviceID))) {
        $removeFailed = $true
    }
}

if ($removeFailed) {
    throw '移除失敗的 WinTun PnP 裝置未完全成功；未刪除 driver store。'
}

$pythonCode = @'
import ctypes
import pathlib

import pytun_pmd3

root = pathlib.Path(pytun_pmd3.__file__).parent
dlls = list(root.rglob("wintun.dll"))
if not dlls:
    print(f"找不到 wintun.dll：{root}")
    raise SystemExit(2)

api = ctypes.WinDLL(str(dlls[0]))
api.WintunDeleteDriver.restype = ctypes.c_bool
ok = bool(api.WintunDeleteDriver())
print(f"WintunDeleteDriver={ok}")
raise SystemExit(0 if ok else 1)
'@

$encodedPythonCode = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($pythonCode))
$pythonBootstrap = 'import base64,sys;exec(base64.b64decode(sys.argv[1]))'
& $PythonExe -c $pythonBootstrap $encodedPythonCode
if ($LASTEXITCODE -ne 0) {
    throw "WintunDeleteDriver 失敗，Python exit $LASTEXITCODE"
}

Write-Host '[LocWarp] driver store 已重置；開始做獨立 WinTun adapter 建立驗證。'
[void](Invoke-PnpUtil @('/scan-devices'))

$probeCode = @'
import sys
import uuid

from pytun_pmd3.wintun import TunTapDevice

name = f"LocWarpRepair-{uuid.uuid4().hex[:12]}"
device = None
try:
    device = TunTapDevice(name)
    print(f"WintunProbe=ok name={name}")
except OSError as exc:
    error_code = getattr(exc, "winerror", None) or getattr(exc, "errno", None)
    print(f"WintunProbe=failed errno={error_code} message={exc}")
    raise SystemExit(4 if error_code in (31, 4319) else 1)
finally:
    if device is not None:
        device.close()
'@

$encodedProbeCode = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($probeCode))
& $PythonExe -c $pythonBootstrap $encodedProbeCode
$probeExit = $LASTEXITCODE

Start-Sleep -Milliseconds 800
[void](Invoke-PnpUtil @('/scan-devices'))
$failedAfterProbe = @(Get-WintunPnpDevice | Where-Object { Test-PnpFailure $_ })
foreach ($device in $failedAfterProbe) {
    Write-Host "  remove failed probe device: $($device.PNPDeviceID)"
    [void](Invoke-PnpUtil @('/remove-device', $device.PNPDeviceID))
}

if ($probeExit -eq 4) {
    Write-Warning 'WinTun adapter 建立仍回傳 Code 31/4319（PnP Code 56）；Windows 仍要求重新啟動以完成 class configuration。此修復沒有重開機。'
    exit 4
}

if ($probeExit -ne 0) {
    throw "獨立 WinTun adapter 建立驗證失敗，Python exit $probeExit"
}

Write-Host '[LocWarp] driver store reset and standalone WinTun probe completed.'
exit 0

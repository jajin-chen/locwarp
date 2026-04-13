from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from models.schemas import DeviceInfo

router = APIRouter(prefix="/api/device", tags=["device"])


def _dm():
    from main import app_state
    return app_state.device_manager


@router.get("/list", response_model=list[DeviceInfo])
async def list_devices():
    dm = _dm()
    return await dm.discover_devices()


# ── WiFi connection ─────────────────────────────────────

class WifiConnectRequest(BaseModel):
    ip: str


@router.post("/wifi/connect")
async def wifi_connect(req: WifiConnectRequest):
    """Connect to an iOS device over WiFi by IP address."""
    from main import app_state
    dm = _dm()
    try:
        info = await dm.connect_wifi(req.ip)
        await app_state.create_engine_for_device(info.udid)
        return {
            "status": "connected",
            "udid": info.udid,
            "name": info.name,
            "ios_version": info.ios_version,
            "connection_type": "Network",
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/wifi/scan")
async def wifi_scan():
    """Scan the local network for iOS devices."""
    dm = _dm()
    try:
        results = await dm.scan_wifi_devices()
        return results
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class WifiTunnelConnectRequest(BaseModel):
    rsd_address: str
    rsd_port: int


@router.post("/wifi/tunnel")
async def wifi_tunnel_connect(req: WifiTunnelConnectRequest):
    """Connect to a device via an existing WiFi tunnel (RSD address/port)."""
    from main import app_state
    dm = _dm()
    try:
        info = await dm.connect_wifi_tunnel(req.rsd_address, req.rsd_port)
        await app_state.create_engine_for_device(info.udid)
        return {
            "status": "connected",
            "udid": info.udid,
            "name": info.name,
            "ios_version": info.ios_version,
            "connection_type": "Network",
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── WiFi Tunnel lifecycle (start / status / stop) ───────

import asyncio
import json
import subprocess
import sys
import logging
from pathlib import Path

_tunnel_logger = logging.getLogger("wifi_tunnel")


class _TunnelManager:
    """Owns the subprocess + info state for the WiFi tunnel, serialised behind
    an asyncio.Lock so concurrent /start or /stop requests never race."""

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.info: dict | None = None
        self.lock = asyncio.Lock()
        self.watchdog_task: asyncio.Task | None = None

    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None


_tunnel = _TunnelManager()


def _kill_stale_tunnel_processes() -> int:
    """Kill any leftover wifi_tunnel.py python processes from previous runs.
    Returns the number of processes killed."""
    killed = 0
    # wmic was removed from Windows 11 — use PowerShell CIM instead.
    ps_script = (
        "Get-CimInstance Win32_Process -Filter \"Name like 'py%.exe' or Name like 'python%.exe'\" "
        "| Where-Object { $_.CommandLine -like '*wifi_tunnel.py*' } "
        "| Select-Object -ExpandProperty ProcessId"
    )
    try:
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command", ps_script],
            stderr=subprocess.DEVNULL, timeout=10, text=True,
        )
        for line in out.splitlines():
            pid = line.strip()
            if not pid.isdigit():
                continue
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", pid],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=5,
                )
                killed += 1
                _tunnel_logger.info("Killed stale tunnel process PID=%s", pid)
            except Exception:
                _tunnel_logger.exception("Failed to kill PID=%s", pid)
    except FileNotFoundError:
        _tunnel_logger.warning("PowerShell not found; skipping stale tunnel cleanup")
    except Exception:
        _tunnel_logger.exception("Failed to enumerate stale tunnel processes")
    # Remove stale info file
    try:
        info_path = Path.home() / ".locwarp" / "wifi_tunnel_info.json"
        if info_path.exists():
            info_path.unlink()
    except OSError:
        _tunnel_logger.debug("Could not remove stale tunnel info file", exc_info=True)
    return killed


class WifiTunnelStartRequest(BaseModel):
    ip: str
    port: int = 49152
    udid: str | None = None


def _get_primary_local_ip() -> str | None:
    """Return this machine's primary IPv4 (the one used to reach the internet)."""
    import socket as _s
    try:
        s = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return None


async def _tcp_probe(ip: str, port: int, timeout: float = 0.4) -> bool:
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, port), timeout=timeout,
        )
        writer.close()
        try:
            await writer.wait_closed()
        except (OSError, ConnectionError):
            pass
        return True
    except (OSError, ConnectionError, asyncio.TimeoutError):
        return False


async def _scan_subnet_for_port(port: int = 49152) -> list[str]:
    """Scan the local /24 subnet for hosts responding on the given TCP port."""
    my_ip = _get_primary_local_ip()
    if not my_ip:
        return []
    try:
        parts = my_ip.split(".")
        prefix = ".".join(parts[:3])
    except (AttributeError, IndexError):
        return []

    candidates = [f"{prefix}.{i}" for i in range(1, 255) if f"{prefix}.{i}" != my_ip]
    results = await asyncio.gather(
        *[_tcp_probe(ip, port, 0.4) for ip in candidates],
        return_exceptions=True,
    )
    hits = [ip for ip, ok in zip(candidates, results) if ok is True]
    return hits


@router.get("/wifi/tunnel/discover")
async def wifi_tunnel_discover():
    """Find iPhones on the local network. First tries mDNS (Bonjour RemotePairing
    broadcast); if that yields nothing, falls back to a /24 subnet TCP scan on the
    standard RemotePairing port (49152)."""
    results: list[dict] = []

    # --- 1) mDNS / Bonjour broadcast ---
    try:
        from pymobiledevice3.bonjour import browse_remotepairing
        instances = await browse_remotepairing(timeout=3.0)
        for inst in instances:
            ipv4s = [a for a in (inst.addresses or []) if ":" not in a]
            addrs = ipv4s if ipv4s else list(inst.addresses or [])
            for addr in addrs:
                results.append({
                    "ip": addr,
                    "port": inst.port,
                    "host": inst.host,
                    "name": inst.instance or inst.host,
                    "method": "mdns",
                })
    except Exception as e:
        _tunnel_logger.warning("mDNS browse failed: %s", e)

    # --- 2) Fallback: TCP subnet scan on port 49152 ---
    if not results:
        _tunnel_logger.info("mDNS empty; falling back to /24 TCP scan on port 49152")
        try:
            hits = await _scan_subnet_for_port(49152)
            for ip in hits:
                results.append({
                    "ip": ip,
                    "port": 49152,
                    "host": ip,
                    "name": ip,
                    "method": "tcp_scan",
                })
        except Exception as e:
            _tunnel_logger.warning("TCP scan failed: %s", e)

    # De-dupe on (ip, port)
    seen = set()
    unique = []
    for r in results:
        key = (r["ip"], r["port"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(r)

    return {"devices": unique}


def _find_python313() -> list[str] | None:
    """Find a Python 3.13+ interpreter on the system.  Returns the
    command as a list of strings suitable for ``subprocess.Popen``."""
    import shutil

    for name in ("py", "python3.13", "python3", "python"):
        path = shutil.which(name)
        if path is None:
            continue
        try:
            cmd = [path, "-3.13", "--version"] if name == "py" else [path, "--version"]
            out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=5)
            ver = out.decode().strip()
            parts = ver.split()[-1].split(".")
            if int(parts[0]) >= 3 and int(parts[1]) >= 13:
                return [path, "-3.13"] if name == "py" else [path]
        except (subprocess.SubprocessError, ValueError, IndexError, OSError):
            continue
    return None


async def _cleanup_wifi_connections() -> list[str]:
    """Disconnect any Network devices + drop the simulation engine.
    Returns the UDIDs that were disconnected."""
    from main import app_state
    dm = _dm()
    udids: list[str] = []
    try:
        udids = [
            udid for udid, conn in list(dm._connections.items())
            if getattr(conn, "connection_type", "") == "Network"
        ]
        for udid in udids:
            try:
                await dm.disconnect(udid)
                _tunnel_logger.info("Disconnected WiFi device %s", udid)
            except (OSError, RuntimeError):
                _tunnel_logger.exception("Failed to disconnect %s", udid)
        if udids and app_state.simulation_engine is not None:
            app_state.simulation_engine = None
    except Exception:
        _tunnel_logger.exception("WiFi cleanup step failed")
    return udids


async def _tunnel_watchdog() -> None:
    """Poll the tunnel subprocess; if it dies unexpectedly (e.g. WiFi blip,
    iPhone locked, admin revoked) while we still think it is running, clean
    up any dependent WiFi connections so the UI can recover gracefully."""
    try:
        while True:
            await asyncio.sleep(2.0)
            proc = _tunnel.proc
            if proc is None:
                return
            if proc.poll() is None:
                continue  # still alive
            _tunnel_logger.warning(
                "Tunnel subprocess exited unexpectedly (code=%s); cleaning up",
                proc.returncode,
            )
            async with _tunnel.lock:
                # Double-check under lock; /stop may have already handled it
                if _tunnel.proc is proc:
                    await _cleanup_wifi_connections()
                    _tunnel.proc = None
                    _tunnel.info = None
                    info_path = Path.home() / ".locwarp" / "wifi_tunnel_info.json"
                    if info_path.exists():
                        try:
                            info_path.unlink()
                        except OSError:
                            pass
                    # Fire a WebSocket event so the frontend can show a banner
                    try:
                        from api.websocket import broadcast
                        await broadcast("tunnel_lost", {"reason": "subprocess_exited"})
                    except Exception:
                        _tunnel_logger.exception("Failed to emit tunnel_lost event")
            return
    except asyncio.CancelledError:
        raise


@router.post("/wifi/tunnel/start")
async def wifi_tunnel_start(req: WifiTunnelStartRequest):
    """Start a WiFi tunnel subprocess (requires Python 3.13+ and admin)."""
    async with _tunnel.lock:
        if _tunnel.is_running():
            if _tunnel.info:
                return {"status": "already_running", **_tunnel.info}
            return {"status": "already_running"}

        stale_killed = _kill_stale_tunnel_processes()
        if stale_killed:
            _tunnel_logger.info("Cleaned up %d stale tunnel process(es)", stale_killed)
            await asyncio.sleep(1.5)

        bundled_tunnel = None
        if getattr(sys, "frozen", False):
            candidate = Path(sys.executable).resolve().parent.parent / "wifi-tunnel" / "wifi-tunnel.exe"
            if candidate.exists():
                bundled_tunnel = candidate

        resolved_udid = req.udid
        if not resolved_udid:
            try:
                dm = _dm()
                conns = list(dm._connections.keys())
                if conns:
                    resolved_udid = conns[0]
            except (RuntimeError, AttributeError):
                pass
        if not resolved_udid:
            resolved_udid = "auto"

        if bundled_tunnel is not None:
            cmd_parts = [str(bundled_tunnel), "--ip", req.ip, "--port", str(req.port), "--udid", resolved_udid]
        else:
            py = _find_python313()
            if py is None:
                raise HTTPException(
                    status_code=500,
                    detail={"code": "python313_missing", "message": "需要 Python 3.13+ 才能啟動 WiFi Tunnel"},
                )
            script = Path(__file__).resolve().parent.parent.parent / "wifi_tunnel.py"
            if not script.exists():
                raise HTTPException(status_code=500, detail={"code": "tunnel_script_missing", "message": f"找不到 wifi_tunnel.py:{script}"})
            cmd_parts = py + [str(script), "--ip", req.ip, "--port", str(req.port), "--udid", resolved_udid]

        _tunnel_logger.info("Starting WiFi tunnel: %s", " ".join(cmd_parts))

        try:
            _tunnel.proc = subprocess.Popen(
                cmd_parts,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except (OSError, FileNotFoundError) as e:
            raise HTTPException(status_code=500, detail={"code": "tunnel_spawn_failed", "message": f"無法啟動 tunnel 進程:{e}"})

        info_path = Path.home() / ".locwarp" / "wifi_tunnel_info.json"
        for _ in range(40):
            await asyncio.sleep(0.5)
            if _tunnel.proc.poll() is not None:
                output = _tunnel.proc.stdout.read() if _tunnel.proc.stdout else ""
                _tunnel.proc = None
                raise HTTPException(
                    status_code=500,
                    detail={"code": "tunnel_exited", "message": f"Tunnel 進程異常結束:{output[-500:]}"},
                )
            if info_path.exists():
                try:
                    data = json.loads(info_path.read_text())
                    _tunnel.info = data
                    _tunnel_logger.info("WiFi tunnel started: %s", data)
                    if _tunnel.watchdog_task is None or _tunnel.watchdog_task.done():
                        _tunnel.watchdog_task = asyncio.create_task(_tunnel_watchdog())
                    return {"status": "started", **data}
                except json.JSONDecodeError:
                    continue

        if _tunnel.is_running():
            try:
                _tunnel.proc.terminate()
            except OSError:
                pass
        _tunnel.proc = None
        raise HTTPException(status_code=500, detail={"code": "tunnel_timeout", "message": "Tunnel 啟動逾時(20 秒)"})


@router.get("/wifi/tunnel/status")
async def wifi_tunnel_status():
    """Check if the WiFi tunnel subprocess is running."""
    if not _tunnel.is_running():
        _tunnel.proc = None
        _tunnel.info = None
        return {"running": False}
    return {"running": True, **(_tunnel.info or {})}


@router.post("/wifi/tunnel/stop")
async def wifi_tunnel_stop():
    """Stop the WiFi tunnel subprocess and clean up any network-based
    device connections that were routed through it."""
    from main import app_state
    dm = _dm()

    async with _tunnel.lock:
        await _cleanup_wifi_connections()

        if not _tunnel.is_running():
            _tunnel.proc = None
            _tunnel.info = None
            return {"status": "not_running"}

        # Cancel watchdog first so it doesn't race on our cleanup
        if _tunnel.watchdog_task and not _tunnel.watchdog_task.done():
            _tunnel.watchdog_task.cancel()

        try:
            _tunnel.proc.terminate()
            try:
                _tunnel.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _tunnel.proc.kill()
        except OSError:
            _tunnel_logger.exception("Failed to terminate tunnel process")

        _tunnel.proc = None
        _tunnel.info = None

    info_path = Path.home() / ".locwarp" / "wifi_tunnel_info.json"
    if info_path.exists():
        try:
            info_path.unlink()
        except OSError:
            pass

    # Try to fall back to USB if a device is still plugged in
    try:
        devices = await dm.discover_devices()
        usb_dev = next((d for d in devices if d.connection_type != "Network"), None)
        if usb_dev:
            await dm.connect(usb_dev.udid)
            await app_state.create_engine_for_device(usb_dev.udid)
            _tunnel_logger.info("Switched back to USB connection: %s", usb_dev.udid)
    except Exception:
        _tunnel_logger.exception("USB fallback after tunnel stop failed")

    return {"status": "stopped"}


@router.post("/wifi/tunnel/start-and-connect")
async def wifi_tunnel_start_and_connect(req: WifiTunnelStartRequest):
    """Start a WiFi tunnel and immediately connect the device through it."""
    from main import app_state

    # Start the tunnel
    tunnel_result = await wifi_tunnel_start(req)
    if tunnel_result.get("status") not in ("started", "already_running"):
        raise HTTPException(status_code=500, detail="Tunnel failed to start")

    rsd_address = tunnel_result.get("rsd_address")
    rsd_port = tunnel_result.get("rsd_port")

    if not rsd_address or not rsd_port:
        raise HTTPException(status_code=500, detail="Tunnel started but no RSD info available")

    # Connect through the tunnel
    dm = _dm()
    try:
        info = await dm.connect_wifi_tunnel(rsd_address, rsd_port)
        await app_state.create_engine_for_device(info.udid)
        return {
            "status": "connected",
            "udid": info.udid,
            "name": info.name,
            "ios_version": info.ios_version,
            "connection_type": "Network",
            "rsd_address": rsd_address,
            "rsd_port": rsd_port,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Tunnel started but connection failed: {e}")


# ── Generic UDID routes (MUST be defined after all specific /wifi/* routes
#    so that /wifi/* paths do not accidentally match {udid}). ─────────────

@router.post("/{udid}/connect")
async def connect_device(udid: str):
    from main import app_state
    dm = _dm()
    try:
        await dm.connect(udid)
        await app_state.create_engine_for_device(udid)
        return {"status": "connected", "udid": udid}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{udid}/connect")
async def disconnect_device(udid: str):
    dm = _dm()
    await dm.disconnect(udid)
    return {"status": "disconnected", "udid": udid}


@router.get("/{udid}/info", response_model=DeviceInfo | None)
async def device_info(udid: str):
    dm = _dm()
    devices = await dm.discover_devices()
    for d in devices:
        if d.udid == udid:
            return d
    raise HTTPException(status_code=404, detail="Device not found")

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from models.schemas import DeviceInfo
from services.tunnel_discovery import (
    _scan_ports_for_ip,
    discover_tunnel_candidates,
    filter_remotepairing_ports,
)
from services.tunnel_manager import (
    TunnelStartAttempt,
    TunnelStartCancelled,
    TunnelStartTimedOut,
    _acquire_tunnel_start_lane_until,
    _await_cleanup_task,
    _candidate_start_fenced_locked,
    _cleanup_wifi_connection_for,
    _detach_tunnel,
    _dm,
    _finish_tunnel_start,
    _get_tunnel_lifecycle_lock,
    _next_tunnel_generation_locked,
    _per_tunnel_watchdog,
    _register_tunnel_start,
    _request_tunnel_stop,
    _start_attempt_fenced_locked,
    _take_tunnel_side_effects_locked,
    _stop_tunnel_plan,
    _stop_tunnel_parts,
    _tunnel_generations,
    _tunnel_watchdogs,
    _tunnels,
    _tunnels_lock,
)

router = APIRouter(prefix="/api/device", tags=["device"])


@router.get("/list", response_model=list[DeviceInfo])
async def list_devices():
    dm = _dm()
    return await dm.discover_devices()


# /wifi/connect (legacy direct-IP WiFi for iOS <17) removed in v0.1.49.


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
    from core.device_manager import UnsupportedIosVersionError
    dm = _dm()
    # Max 3 devices (group mode). connect_wifi_tunnel may reconnect an existing udid;
    # we can only cheaply check the pre-state here.
    if len(dm._connections) >= MAX_DEVICES:
        raise HTTPException(
            status_code=409,
            detail={"code": "max_devices_reached", "message": f"已連接最多 {MAX_DEVICES} 台裝置"},
        )
    connection_lease = None
    connected_udid = None
    connected_engine = None
    adoption_lock = None
    adoption_lock_acquired = False
    runner_snapshot = None
    adoption_committed = False
    was_primary = False

    async def _cleanup_owned_connection() -> None:
        if adoption_committed:
            return
        if connection_lease is None or connected_udid is None:
            return
        await _cleanup_wifi_connection_for(
            connected_udid,
            expected_connection=connection_lease,
            expected_engine=connected_engine,
            caller="wifi_tunnel_connect_failed",
        )

    async def _before_close_previous(
        previous_udid: str,
        _previous,
    ) -> None:
        """Quiesce the exact engine before DeviceManager installs C1."""
        nonlocal was_primary
        was_primary = app_state._primary_udid == previous_udid
        stale_engine = app_state.simulation_engines.get(previous_udid)
        if stale_engine is not None:
            await _cleanup_wifi_connection_for(
                previous_udid,
                expected_connection=None,
                expected_engine=stale_engine,
                caller="wifi_tunnel_connect_engine_rebuild",
                broadcast=False,
            )

    try:
        # Capture the exact runner/key/generation before DM mutates its lease.
        # The RSD tuple is the only stable bridge from this legacy route back
        # to a runner created by /wifi/tunnel/start.
        adoption_lock = _get_tunnel_lifecycle_lock()
        await adoption_lock.acquire()
        adoption_lock_acquired = True
        async with _tunnels_lock:
            for key, runner in _tunnels.items():
                runner_info = runner.info or {}
                try:
                    info_port = int(runner_info.get("rsd_port"))
                except (TypeError, ValueError):
                    continue
                if (
                    runner.is_running()
                    and runner_info.get("rsd_address") == req.rsd_address
                    and info_port == req.rsd_port
                ):
                    runner_snapshot = (
                        key,
                        runner,
                        _tunnel_generations.get(key),
                    )
                    break
        # The fast pre-check above is only an early rejection.  A concurrent
        # direct connect may have consumed the final slot while this request
        # waited for the adoption commit lock, so enforce the cap again at
        # the serialized DM-install boundary.
        if len(dm._connections) >= MAX_DEVICES:
            raise HTTPException(
                status_code=409,
                detail={"code": "max_devices_reached", "message": f"已連接最多 {MAX_DEVICES} 台裝置"},
            )
        info, connection_lease = await dm.connect_wifi_tunnel_owned(
            req.rsd_address,
            req.rsd_port,
            before_close_previous=_before_close_previous,
        )
        connected_udid = info.udid
        try:
            await app_state.create_engine_for_device(info.udid)
        finally:
            connected_engine = app_state.simulation_engines.get(info.udid)
        if (
            was_primary
            and connected_engine is app_state.simulation_engines.get(info.udid)
        ):
            app_state._primary_udid = info.udid

        if runner_snapshot is not None:
            snapshot_key, snapshot_runner, snapshot_generation = runner_snapshot
            old_watchdog = None
            old_side_effects = ()
            rearm_failed = False
            async with _tunnels_lock:
                if (
                    _tunnels.get(snapshot_key) is not snapshot_runner
                    or _tunnel_generations.get(snapshot_key) != snapshot_generation
                ):
                    rearm_failed = True
                else:
                    destination = _tunnels.get(info.udid)
                    if destination is not None and destination is not snapshot_runner:
                        rearm_failed = True
                    else:
                        old_watchdog = _tunnel_watchdogs.pop(snapshot_key, None)
                        old_side_effects = _take_tunnel_side_effects_locked(
                            snapshot_key,
                            generation=snapshot_generation,
                        )
                        if snapshot_key != info.udid:
                            _tunnels.pop(snapshot_key, None)
                            _next_tunnel_generation_locked(snapshot_key)
                        generation = _next_tunnel_generation_locked(info.udid)
                        _tunnels[info.udid] = snapshot_runner
                        _tunnel_watchdogs[info.udid] = _spawn_tunnel_watchdog(
                            info.udid,
                            snapshot_runner,
                            generation,
                            connection_lease=connection_lease,
                            connection_engine=connected_engine,
                        )
        if runner_snapshot is not None and rearm_failed:
            raise RuntimeError(
                "WiFi tunnel runner ownership changed during direct connection"
            )
        if runner_snapshot is not None:
            # The exact runner/generation now owns the new C/E lease.  From
            # this point onward a caller cancellation or broadcast failure
            # must preserve the committed lifecycle; pre-commit failures
            # still use the exact C/E cleanup above.
            adoption_committed = True
            await _stop_tunnel_parts(
                None,
                old_watchdog,
                side_effects=old_side_effects,
                caller="wifi_tunnel_connect_rearm",
                udid=info.udid,
            )
        try:
            from api.websocket import broadcast
            await broadcast("device_connected", {
                "udid": info.udid,
                "name": info.name,
                "ios_version": info.ios_version,
                "connection_type": "Network",
            })
        except Exception:
            pass
        return {
            "status": "connected",
            "udid": info.udid,
            "name": info.name,
            "ios_version": info.ios_version,
            "connection_type": "Network",
        }
    except asyncio.CancelledError:
        await _cleanup_owned_connection()
        raise
    except HTTPException:
        await _cleanup_owned_connection()
        raise
    except UnsupportedIosVersionError as e:
        await _cleanup_owned_connection()
        raise HTTPException(
            status_code=400,
            detail={
                "code": "ios_unsupported",
                "message": (
                    f"偵測到 iOS {e.version},LocWarp 自 v0.1.49 起僅支援 "
                    f"iOS {UnsupportedIosVersionError.MIN_VERSION} 以上。"
                    f"請將裝置升級至 iOS {UnsupportedIosVersionError.MIN_VERSION} 或更新版本後再連線。"
                ),
                "ios_version": e.version,
                "min_version": UnsupportedIosVersionError.MIN_VERSION,
            },
        )
    except Exception as e:
        await _cleanup_owned_connection()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if adoption_lock_acquired:
            adoption_lock.release()


# ── WiFi Tunnel lifecycle (start / status / stop) ───────

import asyncio
import logging

from core.wifi_tunnel import TunnelRunner

_tunnel_logger = logging.getLogger("wifi_tunnel")
def _spawn_tunnel_watchdog(
    udid: str,
    runner: TunnelRunner,
    generation: int | None,
    *,
    connection_lease: object | None = None,
    connection_engine: object | None = None,
) -> asyncio.Task:
    """Create a watchdog while keeping lightweight legacy test doubles valid."""
    try:
        watchdog = _per_tunnel_watchdog(
            udid,
            runner,
            generation,
            connection_lease=connection_lease,
            connection_engine=connection_engine,
        )
    except TypeError:
        # Older integrations/tests may still expose the pre-lease three-
        # argument watchdog.  The production implementation accepts the
        # ownership keywords above; this fallback does not alter it.
        watchdog = _per_tunnel_watchdog(udid, runner, generation)
    return asyncio.create_task(watchdog)

# Group-mode device cap. Same value gates USB auto-connect, /wifi/tunnel,
# /wifi/tunnel/start, /wifi/tunnel/start-and-connect, and /{udid}/connect.
MAX_DEVICES = 3

# The per-device tunnel registry (_tunnels / _tunnel_watchdogs / _tunnels_lock)
# and the watchdog + restart machinery live in services/tunnel_manager.py so
# core/device_manager.py can drive the same restart path without importing
# this router (imported at the top of this file).


@router.post("/wifi/repair")
async def wifi_repair():
    """Regenerate the RemotePairing pair record (~/.pymobiledevice3/) using a
    currently-attached USB device. The iPhone will show a 'Trust This Computer'
    prompt the first time; after the user taps 信任, a fresh RemotePairing
    record is written and WiFi Tunnel will work again.

    Flow:
      1. List USB devices (must have at least one plugged in).
      2. Open a USB lockdown session with autopair=True — this triggers the
         Trust prompt if the Apple Lockdown USB record is missing.
      3. For iOS 17+: open CoreDeviceTunnelProxy.start_tcp_tunnel() briefly.
         pymobiledevice3 persists the RemotePairing record to
         ~/.pymobiledevice3/ as a side effect of the RSD handshake.
    """
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.usbmux import list_devices as mux_list_devices
    from pymobiledevice3.remote.tunnel_service import (
        CoreDeviceTunnelProxy,
        create_core_device_tunnel_service_using_rsd,
    )
    from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService

    try:
        raw_devices = await mux_list_devices()
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={"code": "usbmux_unavailable", "message": f"無法列出 USB 裝置:{e}"},
        )

    # Prefer a USB-attached device (Network entries won't help us regenerate
    # the RemotePairing record).
    usb_dev = next((d for d in raw_devices if getattr(d, "connection_type", "USB") == "USB"), None)
    if usb_dev is None:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "repair_needs_usb",
                "message": "請先用 USB 線連接 iPhone。重新配對需要 USB 觸發『信任這台電腦』提示。",
            },
        )

    udid = usb_dev.serial
    _tunnel_logger.info("Re-pair requested for USB device %s", udid)

    # Step 1: USB lockdown autopair — pops Trust prompt if USB record missing.
    try:
        lockdown = await create_using_usbmux(serial=udid, autopair=True)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={
                "code": "trust_failed",
                "message": f"USB 信任失敗 — 請在 iPhone 解鎖畫面上點「信任」後再試:{e}",
                "udid": udid,
            },
        )

    ios_version = lockdown.all_values.get("ProductVersion", "0.0")
    name = lockdown.all_values.get("DeviceName", "iPhone")

    # Step 2: iOS 17+ — briefly open a CoreDeviceTunnelProxy. The RSD handshake
    # re-generates the ~/.pymobiledevice3/ RemotePairing record.
    try:
        major = int(ios_version.split(".")[0])
    except (ValueError, IndexError):
        major = 0

    remote_record_regenerated = False
    if major >= 17:
        # Delete any stale remote pair record for this udid so the
        # RemotePairingProtocol.connect() path can't short-circuit through
        # the cached (possibly-corrupt) record and actually runs _pair().
        try:
            from pymobiledevice3.common import get_home_folder
            from pymobiledevice3.pair_records import (
                PAIRING_RECORD_EXT,
                get_remote_pairing_record_filename,
            )
            stale = get_home_folder() / f"{get_remote_pairing_record_filename(udid)}.{PAIRING_RECORD_EXT}"
            if stale.exists():
                stale.unlink()
                _tunnel_logger.info("Re-pair: removed stale remote pair record %s", stale)
        except Exception:
            _tunnel_logger.debug("Re-pair: could not check/remove stale pair record", exc_info=True)

        proxy = None
        tunnel_ctx = None
        rsd = None
        tunnel_svc = None
        try:
            # 1. Open a CoreDeviceTunnelProxy tunnel over USB.
            proxy = await CoreDeviceTunnelProxy.create(lockdown)
            tunnel_ctx = proxy.start_tcp_tunnel()
            tunnel_result = await tunnel_ctx.__aenter__()

            # 2. Construct an RSD on the tunnel.
            rsd = RemoteServiceDiscoveryService((tunnel_result.address, tunnel_result.port))
            await rsd.connect()

            # 3. This is the step that actually triggers the Trust dialog
            #    (when no cached record) and persists the RemotePairing file
            #    to ~/.pymobiledevice3/. RemotePairingProtocol.connect()
            #    calls _pair() which runs _request_pair_consent() — Trust
            #    prompt — then save_pair_record().
            _tunnel_logger.info(
                "Re-pair: opening CoreDeviceTunnelService over RSD %s:%s — "
                "Trust prompt should appear on iPhone...",
                tunnel_result.address, tunnel_result.port,
            )
            tunnel_svc = await create_core_device_tunnel_service_using_rsd(rsd, autopair=True)
            _tunnel_logger.info(
                "Re-pair: CoreDeviceTunnelService connected for %s — RemotePairing record written",
                udid,
            )
            remote_record_regenerated = True
        except Exception as e:
            _tunnel_logger.exception("Re-pair: RemotePairing handshake failed")
            msg = str(e)
            if "PairingDialogResponsePending" in msg or "consent" in msg.lower():
                friendly = "請在 iPhone 解鎖螢幕上按「信任」後重試(timeout 只有幾秒)。"
            elif "not paired" in msg.lower() or "pairingerror" in msg.lower():
                friendly = "USB 配對失效,請拔 USB 重插一次並按信任。"
            else:
                friendly = f"RemotePairing 握手失敗:{msg}"
            raise HTTPException(
                status_code=500,
                detail={
                    "code": "remote_pair_failed",
                    "message": friendly,
                    "udid": udid,
                    "ios_version": ios_version,
                },
            )
        finally:
            # Close everything in reverse order; ignore errors.
            for closer in (
                lambda: tunnel_svc and tunnel_svc.close(),
                lambda: rsd and rsd.close(),
                lambda: tunnel_ctx and tunnel_ctx.__aexit__(None, None, None),
            ):
                try:
                    r = closer()
                    if hasattr(r, "__await__"):
                        await r
                except Exception:
                    pass
            try:
                if proxy is not None:
                    proxy.close()
            except Exception:
                pass

    return {
        "status": "paired",
        "udid": udid,
        "name": name,
        "ios_version": ios_version,
        "remote_record_regenerated": remote_record_regenerated,
    }


class WifiTunnelStartRequest(BaseModel):
    ip: str
    port: int = 49152
    udid: str | None = None
    # Extra RemotePairing port candidates, in fallback order. iOS can pick a
    # different RemotePairing port after every reboot or network rebind, so a
    # remembered port is only a starting hint.
    ports: list[int] | None = None


# Upstream /wifi/tunnel/start candidate-walk budget, including its one live
# rescan. The composite start-and-connect route reuses candidate resolution,
# but its DM connect and engine setup run after this phase and are not part of
# this candidate-walk budget.
TUNNEL_START_BUDGET = 45.0


class WifiTunnelFindPortRequest(BaseModel):
    ip: str


@router.post("/wifi/tunnel/find_port")
async def wifi_tunnel_find_port(req: WifiTunnelFindPortRequest):
    """Scan an iPhone IP across the IANA dynamic range (49152-65535) and return
    every open TCP port. Used as the manual fallback when mDNS / Bonjour fails
    because the user's router blocks multicast or the PC has VPN / virtual NICs
    that hijack the broadcast path."""
    ip = (req.ip or "").strip()
    if not ip:
        raise HTTPException(status_code=400, detail="ip required")
    try:
        ports = filter_remotepairing_ports(await _scan_ports_for_ip(ip))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"ip": ip, "ports": ports}


@router.get("/wifi/tunnel/discover")
async def wifi_tunnel_discover():
    """Find iPhones on the local network. First tries mDNS (Bonjour RemotePairing
    broadcast); if that yields nothing, falls back to a smart /24 subnet scan."""
    devices = await discover_tunnel_candidates()
    # A TCP scan reports one entry per open port. Collapse those entries by
    # IP so the frontend's device cap counts phones, not listeners, and pass
    # the remaining ports as backend handshake hints.
    grouped: dict[str, dict] = {}
    for device in devices:
        ip = str(device.get("ip") or "").strip()
        if not ip:
            continue
        ports = filter_remotepairing_ports([
            device.get("port"), *(device.get("ports") or []),
        ])
        if not ports:
            continue
        entry = grouped.get(ip)
        if entry is None:
            entry = dict(device)
            entry["ip"] = ip
            entry["port"] = ports[0]
            entry["ports"] = ports[:8]
            grouped[ip] = entry
            continue
        existing = entry.setdefault("ports", [])
        for port in ports:
            if port not in existing and len(existing) < 8:
                existing.append(port)
    return {"devices": list(grouped.values())}


def _build_tunnel_udid_candidates(req: WifiTunnelStartRequest) -> list[str]:
    """Return udids to try for an incoming /wifi/tunnel/start request,
    in priority order:

    1. The udid the caller explicitly passed (always trusted)
    2. Currently USB-tracked udids (most likely correct in single-device
       use, and for the dual-device USB+WiFi flow)
    3. Cached pair records under ~/.pymobiledevice3/, sorted by mtime
       (most recently used first) — needed when the user opens LocWarp
       without USB and just types an IP

    The list is de-duped while preserving order. Caller iterates them;
    pair-verify fails fast (~200-400ms) on a wrong identifier so trying
    several is cheap. Bug history: v0.2.92 only used the first candidate,
    which broke multi-iPhone users whose target's pair record happened
    to not be the most-recently-used one."""
    candidates: list[str] = []

    def _add(c: str | None) -> None:
        if c and c not in candidates:
            candidates.append(c)

    _add(req.udid)
    try:
        dm = _dm()
        for u in dm._connections.keys():
            _add(u)
    except (RuntimeError, AttributeError):
        pass
    try:
        from pymobiledevice3.pair_records import iter_remote_pair_records
        records = sorted(
            iter_remote_pair_records(),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for rec in records:
            stem = rec.name
            if stem.startswith("remote_"):
                stem = stem.split("remote_", 1)[1]
            ident = stem.split(".", 1)[0]
            _add(ident)
    except Exception:
        _tunnel_logger.debug("Could not enumerate cached pair records", exc_info=True)

    if not candidates:
        candidates.append(f"pending:{req.ip}:{req.port}")
    return candidates


def _build_tunnel_port_candidates(req: WifiTunnelStartRequest) -> list[int]:
    """Return requested and hinted ports in priority order.

    Invalid, duplicate, and known non-RemotePairing ports are ignored. An
    empty result is valid: the start loop will perform its one live rescan.
    """
    ports: list[int] = []
    for raw in [req.port, *(req.ports or [])]:
        try:
            port = int(raw)
        except (TypeError, ValueError):
            continue
        if port <= 0 or port > 65535:
            continue
        if port in ports:
            continue
        ports.append(port)
    return filter_remotepairing_ports(ports)


async def _wifi_tunnel_start_impl(
    req: WifiTunnelStartRequest,
    attempt: TunnelStartAttempt,
    *,
    lane_already_held: bool = False,
):
    """Start an in-process WiFi tunnel for one device (requires admin).

    The runner is keyed in _tunnels by the actual udid once we resolve
    which paired iPhone is at the requested IP/port. Resolution iterates
    candidate udids (req.udid > USB-tracked > cached pair records) and
    keeps the one whose pair-verify handshake actually succeeds. The
    tunnel cap is enforced separately from the device cap so we don't
    accidentally start a 4th tunnel while only 3 devices are visible to
    dm._connections."""
    # Serialize competing start resolutions for this target IP without
    # blocking different IPs. The caller registered ``attempt`` before
    # waiting for the lane, so an explicit stop can fence this request while
    # the handshake is still unregistered.
    async with _acquire_tunnel_start_lane_until(
        req.ip,
        attempt.deadline,
        already_held=lane_already_held,
    ):
        candidates = _build_tunnel_udid_candidates(req)
        port_candidates = _build_tunnel_port_candidates(req)
        _tunnel_logger.info(
            "WiFi tunnel start: ip=%s ports=%s candidates=%s",
            req.ip, port_candidates or [req.port], candidates,
        )

        async with _tunnels_lock:
            live_count = sum(1 for r in _tunnels.values() if r.is_running())
        if live_count >= MAX_DEVICES:
            raise HTTPException(
                status_code=409,
                detail={"code": "max_devices_reached", "message": f"已連接最多 {MAX_DEVICES} 台裝置"},
            )

        last_error: Exception | None = None
        tried_ports: set[int] = set()
        rescanned = False
        budget_exhausted = False
        loop = asyncio.get_running_loop()
        deadline = attempt.deadline or (loop.time() + TUNNEL_START_BUDGET)

        while True:
            while port_candidates and not budget_exhausted:
                port = port_candidates.pop(0)
                if port in tried_ports:
                    continue
                tried_ports.add(port)

                for cand in candidates:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        budget_exhausted = True
                        break

                    # Inspect/detach registry entries atomically, but stop
                    # stale lifecycle objects after releasing the lock.
                    stale_runner = None
                    stale_wd = None
                    stale_side_effects = ()
                    same_result: dict | None = None
                    busy = False
                    candidate_fenced = False
                    async with _tunnels_lock:
                        attempt.current_udid = cand
                        if _start_attempt_fenced_locked(attempt):
                            raise TunnelStartCancelled
                        if _candidate_start_fenced_locked(attempt, cand):
                            candidate_fenced = True
                        else:
                            existing = _tunnels.get(cand)
                            if existing is not None and existing.is_running():
                                if existing.target_ip == req.ip and existing.target_port == port:
                                    same_result = {
                                        "status": "already_running",
                                        "udid": cand,
                                        "port": existing.target_port,
                                        **(existing.info or {}),
                                    }
                                else:
                                    busy = True
                            elif existing is not None:
                                stale_runner = _tunnels.pop(cand, None)
                                stale_wd = _tunnel_watchdogs.pop(cand, None)
                                stale_side_effects = _take_tunnel_side_effects_locked(cand)
                                if stale_runner is not None:
                                    _next_tunnel_generation_locked(cand)
                    if candidate_fenced:
                        _tunnel_logger.info(
                            "Skipping candidate %s after its stop watermark", cand,
                        )
                        continue
                    if (
                        stale_runner is not None
                        or stale_wd is not None
                        or stale_side_effects
                    ):
                        await _stop_tunnel_parts(
                            stale_runner, stale_wd,
                            side_effects=stale_side_effects,
                            caller="start_replace_stale", udid=cand,
                        )
                    if same_result is not None:
                        # This is an idempotent retry against the live
                        # runner already registered for this exact endpoint.
                        # Carry that runner forward only as a borrowed
                        # reference so start-and-connect can re-arm its
                        # watchdog with the newly acquired C/E lease without
                        # taking ownership of (or later stopping) the runner.
                        attempt.runner = existing
                        attempt.registry_key = cand
                        attempt.runner_owned = False
                        attempt.committed = True
                        return same_result
                    if busy:
                        _tunnel_logger.debug(
                            "Skipping candidate %s: already tunneling elsewhere "
                            "(requested %s:%s)", cand, req.ip, port,
                        )
                        continue

                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        budget_exhausted = True
                        break
                    attempt_timeout = min(8.0, remaining)
                    _tunnel_logger.info(
                        "Trying WiFi tunnel with udid=%s ip=%s port=%d timeout=%.2fs",
                        cand, req.ip, port, attempt_timeout,
                    )

                    runner = TunnelRunner()
                    candidate_fenced = False
                    async with _tunnels_lock:
                        if _start_attempt_fenced_locked(attempt):
                            raise TunnelStartCancelled
                        attempt.runner = runner
                        attempt.runner_owned = True
                        if _candidate_start_fenced_locked(attempt, cand):
                            candidate_fenced = True
                    if candidate_fenced:
                        await _stop_tunnel_parts(
                            runner,
                            None,
                            caller="tunnel_candidate_cancelled",
                            udid=cand,
                        )
                        continue
                    probe_task = asyncio.create_task(
                        runner.start(cand, req.ip, port, timeout=attempt_timeout),
                    )
                    async with _tunnels_lock:
                        if _start_attempt_fenced_locked(attempt):
                            candidate_fenced = True
                        elif _candidate_start_fenced_locked(attempt, cand):
                            candidate_fenced = True
                        else:
                            attempt.probe_task = probe_task
                            attempt.probe_udid = cand
                    if candidate_fenced:
                        probe_task.cancel()
                    try:
                        info = await probe_task
                    except asyncio.TimeoutError as e:
                        last_error = e
                        await _stop_tunnel_parts(
                            runner,
                            None,
                            caller="tunnel_start_timeout",
                            udid=cand,
                        )
                        _tunnel_logger.warning(
                            "WiFi tunnel timed out for udid=%s on port %d; "
                            "trying the next udid/port",
                            cand, port,
                        )
                        # Timeout belongs to this UDID attempt only. Continue
                        # with other pair records on the same port.
                        continue
                    except asyncio.CancelledError:
                        # A per-UDID stop cancels only the child handshake;
                        # the parent route remains alive and may try another
                        # pair record. A hard route fence or caller
                        # cancellation still exits after the runner is
                        # drained.
                        async with _tunnels_lock:
                            hard_fenced = _start_attempt_fenced_locked(attempt)
                            candidate_cancelled = (
                                not hard_fenced
                                and _candidate_start_fenced_locked(attempt, cand)
                            )
                        await _stop_tunnel_parts(
                            runner,
                            None,
                            caller=(
                                "tunnel_candidate_cancelled"
                                if candidate_cancelled
                                else "tunnel_start_cancelled"
                            ),
                            udid=cand,
                        )
                        if candidate_cancelled:
                            continue
                        raise
                    except Exception as e:
                        last_error = e
                        await _stop_tunnel_parts(
                            runner,
                            None,
                            caller="tunnel_start_failed",
                            udid=cand,
                        )
                        _tunnel_logger.info(
                            "WiFi tunnel candidate %s on port %d failed (%s); "
                            "trying next",
                            cand, port, type(e).__name__,
                        )
                        continue
                    finally:
                        async with _tunnels_lock:
                            if attempt.probe_task is probe_task:
                                attempt.probe_task = None
                                attempt.probe_udid = None

                    # The runner may complete right at the deadline even
                    # though its individual timeout was bounded by the
                    # remaining budget. Do not commit a late success.
                    if loop.time() >= deadline:
                        budget_exhausted = True
                        await _stop_tunnel_parts(
                            runner,
                            None,
                            caller="tunnel_start_deadline",
                            udid=cand,
                        )
                        break

                    # Recheck ownership and cap only at this short commit;
                    # no network I/O occurs while _tunnels_lock is held.
                    conflict: str | None = None
                    existing_result: dict | None = None
                    existing_result_runner = None
                    stale_after_runner = None
                    stale_after_wd = None
                    stale_after_side_effects = ()
                    async with _get_tunnel_lifecycle_lock():
                        async with _tunnels_lock:
                            attempt.current_udid = cand
                            if _start_attempt_fenced_locked(attempt):
                                conflict = "cancelled"
                            elif _candidate_start_fenced_locked(attempt, cand):
                                conflict = "candidate_cancelled"
                            else:
                                existing = _tunnels.get(cand)
                                live_count = sum(1 for r in _tunnels.values() if r.is_running())
                                if existing is not None and existing.is_running() and existing is not runner:
                                    if existing.target_ip == req.ip and existing.target_port == port:
                                        conflict = "same"
                                        existing_result_runner = existing
                                        existing_result = {
                                            "status": "already_running",
                                            "udid": cand,
                                            "port": existing.target_port,
                                            **(existing.info or {}),
                                        }
                                    else:
                                        conflict = "busy"
                                elif live_count >= MAX_DEVICES:
                                    conflict = "cap"
                                else:
                                    if existing is not None and existing is not runner:
                                        stale_after_runner = _tunnels.pop(cand, None)
                                        stale_after_wd = _tunnel_watchdogs.pop(cand, None)
                                        stale_after_side_effects = _take_tunnel_side_effects_locked(cand)
                                        if stale_after_runner is not None:
                                            _next_tunnel_generation_locked(cand)
                                    _tunnels[cand] = runner
                                    generation = _next_tunnel_generation_locked(cand)
                                    _tunnel_watchdogs[cand] = _spawn_tunnel_watchdog(
                                        cand, runner, generation,
                                    )
                                    attempt.registry_key = cand
                                    attempt.committed = True
                    if conflict is not None:
                        await _stop_tunnel_parts(
                            runner,
                            None,
                            caller="tunnel_candidate_commit_conflict",
                            udid=cand,
                        )
                        if conflict == "cancelled":
                            raise TunnelStartCancelled
                        if conflict == "candidate_cancelled":
                            continue
                        if conflict == "same" and existing_result is not None:
                            # The runner that won the commit race is borrowed
                            # by this idempotent request; only its watchdog
                            # lease may be rebound later.
                            async with _tunnels_lock:
                                attempt.runner = existing_result_runner
                                attempt.registry_key = cand
                                attempt.runner_owned = False
                                attempt.committed = True
                            return existing_result
                        if conflict == "cap":
                            raise HTTPException(
                                status_code=409,
                                detail={"code": "max_devices_reached", "message": f"已連接最多 {MAX_DEVICES} 台裝置"},
                            )
                        continue
                    if (
                        stale_after_runner is not None
                        or stale_after_wd is not None
                        or stale_after_side_effects
                    ):
                        await _stop_tunnel_parts(
                            stale_after_runner, stale_after_wd,
                            side_effects=stale_after_side_effects,
                            caller="start_replace_stale_after_handshake", udid=cand,
                        )
                    _tunnel_logger.info(
                        "WiFi tunnel started for %s on port %d: %s", cand, port, info,
                    )
                    return {"status": "started", "udid": cand, "port": port, **info}

            if budget_exhausted or rescanned:
                break

            # The remembered/requested candidates are exhausted. Run exactly
            # one live scan, bounded by the same global request deadline.
            rescanned = True
            remaining = deadline - loop.time()
            if remaining <= 0:
                budget_exhausted = True
                break
            _tunnel_logger.info(
                "All known ports failed for %s; re-scanning 49152-65535 "
                "for a live RemotePairing port (%.2fs left)",
                req.ip, remaining,
            )
            try:
                fresh = filter_remotepairing_ports(await asyncio.wait_for(
                    _scan_ports_for_ip(req.ip), timeout=remaining,
                ))
            except asyncio.TimeoutError as e:
                last_error = e
                budget_exhausted = True
                break
            except Exception as e:
                _tunnel_logger.warning("Re-scan of %s failed: %s", req.ip, e)
                fresh = []
            fresh = [p for p in fresh if p not in tried_ports]
            if not fresh:
                break
            _tunnel_logger.info("Re-scan found new port candidates: %s", fresh[:8])
            port_candidates.extend(fresh[:8])

        # A hard budget exhaustion always reports timeout, regardless of the
        # last individual candidate's exception type.
        if budget_exhausted or loop.time() >= deadline:
            raise HTTPException(
                status_code=500,
                detail={"code": "tunnel_timeout", "message": "Tunnel 啟動逾時"},
            ) from last_error
        if isinstance(last_error, asyncio.TimeoutError):
            raise HTTPException(
                status_code=500,
                detail={"code": "tunnel_timeout", "message": "Tunnel 啟動逾時"},
            ) from last_error
        msg = f"無法啟動 tunnel:{last_error}" if last_error else "無法啟動 tunnel"
        raise HTTPException(
            status_code=500,
            detail={"code": "tunnel_spawn_failed", "message": msg},
        )


@router.post("/wifi/tunnel/start")
async def wifi_tunnel_start(req: WifiTunnelStartRequest):
    """Register, run, and finally release one standalone start attempt."""
    deadline = asyncio.get_running_loop().time() + TUNNEL_START_BUDGET
    attempt = await _register_tunnel_start(req.udid, deadline=deadline)
    success = False
    try:
        result = await _wifi_tunnel_start_impl(req, attempt)
        success = True
        return result
    except TunnelStartTimedOut as exc:
        raise HTTPException(
            status_code=500,
            detail={"code": "tunnel_timeout", "message": "Tunnel 啟動逾時"},
        ) from exc
    except TunnelStartCancelled as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "tunnel_start_cancelled",
                "message": "Tunnel 啟動已被停止要求取消",
            },
        ) from exc
    except asyncio.CancelledError:
        if attempt.cancel_requested:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "tunnel_start_cancelled",
                    "message": "Tunnel 啟動已被停止要求取消",
                },
            )
        raise
    finally:
        await _finish_tunnel_start(attempt, success=success)


@router.get("/wifi/tunnel/status")
async def wifi_tunnel_status():
    """Return all active WiFi tunnels with their RSD info.

    The response shape is forward-compatible: the canonical payload is
    `{"tunnels": [{"udid", "rsd_address", "rsd_port", ...}, ...]}`. Legacy
    fields (`running`, `rsd_address`, `rsd_port`) mirror the FIRST tunnel
    so older single-tunnel callers keep working until they migrate."""
    tunnels: list[dict] = []
    for udid, runner in list(_tunnels.items()):
        if not runner.is_running():
            continue
        tunnels.append({
            "udid": udid,
            "ip": runner.target_ip,
            "port": runner.target_port,
            **(runner.info or {}),
        })

    legacy = {"running": len(tunnels) > 0}
    if tunnels:
        legacy.update({k: v for k, v in tunnels[0].items() if k != "udid"})
    return {"tunnels": tunnels, **legacy}


class WifiTunnelStopRequest(BaseModel):
    udid: str | None = None  # None = stop ALL tunnels (legacy stop-all path)


@router.post("/wifi/tunnel/stop")
async def wifi_tunnel_stop(req: WifiTunnelStopRequest | None = None):
    """Stop a specific WiFi tunnel by udid, or all if udid is None.

    Per-udid stop tears down only the named tunnel and its DM
    connection — other tunnels keep running. The legacy stop-all path
    (no udid) preserves prior single-tunnel behaviour for callers that
    haven't migrated yet."""
    target_udid = req.udid if req else None
    dm = _dm()

    _tunnel_logger.warning(
        "/wifi/tunnel/stop endpoint hit. target_udid=%s, active_tunnels=%d, network_conns=%d",
        target_udid,
        sum(1 for r in _tunnels.values() if r.is_running()),
        sum(1 for c in dm._connections.values() if getattr(c, "connection_type", "") == "Network"),
    )

    # Record the stop watermark and cancel matching unregistered starts first.
    # This intentionally does not wait for _tunnel_start_lock or any network
    # handshake. The helper also includes pending registry keys and resolved
    # udids so start-and-connect cannot re-key a runner after this fence.
    plan = await _request_tunnel_stop(target_udid, dm=dm)
    if not plan.udids:
        if plan.matched_pending:
            await _stop_tunnel_plan(plan, caller="wifi_tunnel_stop_endpoint")
            return {"status": "stopped", "udids": []}
        if target_udid is not None:
            return {"status": "not_running", "udid": target_udid}
        return {"status": "not_running"}

    # Snapshot for the USB fallback step below: only re-attach via USB
    # the udids that just had a WiFi conn here, AND skip pending: keys
    # which were only ever placeholders.
    was_network_udids = [
        part.udid for part in plan.parts
        if part.expected_connection is not None
        and not part.udid.startswith("pending:")
    ]

    # Network cleanup and runner stop can wait on device I/O; never hold the
    # registry lock across those operations.
    await _stop_tunnel_plan(plan, caller="wifi_tunnel_stop_endpoint")

    # USB fallback: only re-attach udids that were just in WiFi AND show
    # up as USB right now (covers users plugging in a cable mid-stop).
    try:
        from main import app_state
        devices = await dm.discover_devices()
        for udid in was_network_udids:
            # The exact lease may have lost a CAS race to a newer start.  Do
            # not run USB fallback over that replacement connection.
            current = dm._connections.get(udid)
            if current is not None and getattr(current, "connection_type", "") == "Network":
                continue
            usb_dev = next(
                (d for d in devices if d.udid == udid and d.connection_type == "USB"),
                None,
            )
            if usb_dev is None:
                _tunnel_logger.info(
                    "USB fallback: skipping %s (not visible as USB after tunnel stop)",
                    udid,
                )
                continue
            try:
                await dm.connect(usb_dev.udid)
            except Exception:
                _tunnel_logger.exception("USB fallback: connect failed for %s", usb_dev.udid)
                continue
            try:
                app_state.simulation_engines.pop(usb_dev.udid, None)
                await app_state.create_engine_for_device(usb_dev.udid)
                _tunnel_logger.info("Switched back to USB connection: %s", usb_dev.udid)
            except Exception:
                _tunnel_logger.exception(
                    "USB fallback: engine creation failed for %s; rolling back",
                    usb_dev.udid,
                )
                try:
                    await dm.disconnect(usb_dev.udid)
                except Exception:
                    pass
                app_state.simulation_engines.pop(usb_dev.udid, None)
                if app_state._primary_udid == usb_dev.udid:
                    remaining = next(iter(app_state.simulation_engines.keys()), None)
                    app_state._primary_udid = remaining
                try:
                    from api.websocket import broadcast
                    await broadcast("device_error", {
                        "udid": usb_dev.udid,
                        "stage": "usb_fallback",
                        "error": "USB fallback engine creation failed",
                    })
                except Exception:
                    pass
    except Exception:
        _tunnel_logger.exception("USB fallback after tunnel stop failed")

    return {"status": "stopped", "udids": plan.udids}


async def _wifi_tunnel_start_and_connect_impl(
    req: WifiTunnelStartRequest,
    attempt: TunnelStartAttempt,
    *,
    lane_already_held: bool = False,
):
    """Start a WiFi tunnel and immediately connect the device through it.

    Re-keys the runner from any temporary IP-based key to the real udid
    after dm.connect_wifi_tunnel reveals the device identity. This is the
    primary entrypoint the frontend uses; /start and /wifi/tunnel exist as
    separate primitives but are not chained from the UI today."""
    from main import app_state
    success = False
    tunnel_result: dict | None = None
    started_here = False
    rsd_address = None
    rsd_port = None
    temp_key = None
    connected_udid: str | None = None
    connected_lease = None
    connected_engine = None
    was_primary = False
    adoption_lock = None
    adoption_lock_acquired = False

    async def _cleanup_owned_start_impl() -> None:
        """Detach this request's runner and close any DM connection it made."""
        detached_runner = None
        detached_wd = None
        detached_side_effects = ()
        owned_runner = attempt.runner if started_here else None
        if started_here and temp_key and owned_runner is not None:
            try:
                detached_runner, detached_wd, detached_side_effects = await _detach_tunnel(
                    temp_key,
                    expected=owned_runner,
                )
                if detached_runner is None and connected_udid and connected_udid != temp_key:
                    detached_runner, detached_wd, detached_side_effects = await _detach_tunnel(
                        connected_udid,
                        expected=owned_runner,
                    )
                if detached_runner is None:
                    # A stop plan may already have removed the registry entry;
                    # the attempt still owns this uncommitted/just-committed
                    # runner until its finalizer drains it.
                    detached_runner = owned_runner
                await _stop_tunnel_parts(
                    None, detached_wd,
                    side_effects=detached_side_effects,
                    caller="start_and_connect_failed", udid=temp_key,
                )
            except Exception:
                pass
        if connected_udid and connected_lease is not None:
            try:
                await _cleanup_wifi_connection_for(
                    connected_udid,
                    expected_connection=connected_lease,
                    expected_engine=connected_engine,
                    caller="start_and_connect_failed",
                    broadcast=not attempt.cancel_requested,
                )
            except Exception:
                pass
        if started_here:
            await _stop_tunnel_parts(
                detached_runner, None,
                caller="start_and_connect_failed",
                udid=connected_udid or temp_key or "<unknown>",
            )

    async def _cleanup_owned_start() -> None:
        """Drain owned-start cleanup even when the caller is cancelling."""
        cleanup = asyncio.create_task(_cleanup_owned_start_impl())
        _cancelled, error = await _await_cleanup_task(cleanup)
        if error is not None and not isinstance(error, asyncio.CancelledError):
            _tunnel_logger.error(
                "start-and-connect cleanup failed",
                exc_info=(type(error), error, error.__traceback__),
            )

    async def _before_close_previous(
        previous_udid: str,
        _previous,
    ) -> None:
        """Quiesce the exact engine before DeviceManager installs C1."""
        nonlocal was_primary
        was_primary = app_state._primary_udid == previous_udid
        stale_engine = app_state.simulation_engines.get(previous_udid)
        if stale_engine is not None:
            await _cleanup_wifi_connection_for(
                previous_udid,
                expected_connection=None,
                expected_engine=stale_engine,
                caller="start_and_connect_engine_rebuild",
                broadcast=False,
            )

    try:
        # Cap check before we even spawn a runner. Counts active runners,
        # not dm._connections — a tunnel that's mid-handshake but not yet
        # registered as a device connection still consumes a slot.
        async with _tunnels_lock:
            live_count = sum(1 for r in _tunnels.values() if r.is_running())
            if live_count >= MAX_DEVICES:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "max_devices_reached", "message": f"已連接最多 {MAX_DEVICES} 台裝置"},
                )

        tunnel_result = await _wifi_tunnel_start_impl(
            req,
            attempt,
            lane_already_held=lane_already_held,
        )
        if tunnel_result.get("status") not in ("started", "already_running"):
            raise HTTPException(status_code=500, detail="Tunnel failed to start")

        tunnel_status = tunnel_result.get("status")
        started_here = tunnel_status == "started"
        rsd_address = tunnel_result.get("rsd_address")
        rsd_port = tunnel_result.get("rsd_port")
        temp_key = tunnel_result.get("udid")

        # Candidate probing/runner.start is complete.  Serialize only the
        # adoption commit (DM lease, engine, registry re-key and watchdog
        # lease), leaving different IPs free to probe in parallel.
        adoption_lock = _get_tunnel_lifecycle_lock()
        await adoption_lock.acquire()
        adoption_lock_acquired = True

        # These checks must stay inside the cleanup scope. A successful
        # /start has already registered a runner and watchdog; rejecting a
        # missing RSD payload or a newly-full device cap here must not leave
        # that temporary tunnel alive. Conversely, an already-running
        # result belongs to a pre-existing tunnel and must not be torn down
        # by this request's failure path.
        if not rsd_address or not rsd_port:
            raise HTTPException(status_code=500, detail="Tunnel started but no RSD info available")

        dm = _dm()
        existing_connection = dm._connections.get(temp_key) if temp_key else None
        existing_engine = app_state.simulation_engines.get(temp_key) if temp_key else None
        if (
            tunnel_status == "already_running"
            and req.udid != temp_key
            and existing_connection is not None
            and existing_engine is not None
            and getattr(existing_connection, "connection_type", None) == "Network"
        ):
            # An offline-device retry may resolve to a different device's
            # already-live tunnel.  That tunnel, DM lease, and engine are a
            # complete connection already; reconnecting it would stop the
            # active primary simulation and replace its positioned engine.
            async with _tunnels_lock:
                attempt.resolved_udid = temp_key
                if _start_attempt_fenced_locked(attempt):
                    raise TunnelStartCancelled
            success = True
            return {
                "status": "connected",
                "udid": temp_key,
                "name": tunnel_result.get("name") or getattr(existing_connection, "name", "iPhone"),
                "ios_version": tunnel_result.get("ios_version") or getattr(existing_connection, "ios_version", "0.0"),
                "connection_type": "Network",
                "port": tunnel_result.get("port", req.port),
                "rsd_address": rsd_address,
                "rsd_port": rsd_port,
            }

        if len(dm._connections) >= MAX_DEVICES:
            raise HTTPException(
                status_code=409,
                detail={"code": "max_devices_reached", "message": f"已連接最多 {MAX_DEVICES} 台裝置"},
            )

        info, connected_lease = await dm.connect_wifi_tunnel_owned(
            rsd_address,
            rsd_port,
            before_close_previous=_before_close_previous,
        )
        connected_udid = info.udid
        async with _tunnels_lock:
            # Keep current_udid as the pair-record candidate and record the
            # actual identity separately. A per-UDID stop may target either
            # side of this transition while dm.connect is in flight.
            attempt.resolved_udid = info.udid
            if _start_attempt_fenced_locked(attempt):
                raise TunnelStartCancelled
        # v0.2.60: Drop the stale engine from the prior USB conn so
        # create_engine_for_device rebuilds a fresh one bound to the new
        # WiFi RSD. v0.2.57 made create_engine_for_device idempotent (to
        # survive the watchdog loop wiping current_position), but that
        # means on a USB→WiFi conn switch it would keep the old engine —
        # whose location_service._lockdown still points at the now-closed
        # USB RSD. First teleport over WiFi would then throw
        # ConnectionTerminatedError, reconnect would fail because the
        # cached lockdown is dead, and the user would see the device get
        # kicked as device_lost within 8 seconds of the WiFi switch.
        try:
            await app_state.create_engine_for_device(info.udid)
        finally:
            connected_engine = app_state.simulation_engines.get(info.udid)
        if (
            was_primary
            and connected_engine is app_state.simulation_engines.get(info.udid)
        ):
            app_state._primary_udid = info.udid

        # Re-key the runner from temp_key (often "pending:ip:port") to
        # the real udid so per-udid stop / status / watchdog keep working.
        if temp_key and temp_key == info.udid:
            # The resolver may already have committed the runner under its
            # real UDID.  It still armed a lease-less observer before DM
            # connect, so replace that observer with a fresh generation that
            # carries the exact connection/engine identities.
            old_wd = None
            old_side_effects = ()
            rearm_cancelled = False
            async with _tunnels_lock:
                expected_runner = attempt.runner
                if (
                    _start_attempt_fenced_locked(attempt)
                    or expected_runner is None
                    or _tunnels.get(info.udid) is not expected_runner
                ):
                    rearm_cancelled = True
                else:
                    old_wd = _tunnel_watchdogs.pop(info.udid, None)
                    old_side_effects = _take_tunnel_side_effects_locked(info.udid)
                    generation = _next_tunnel_generation_locked(info.udid)
                    _tunnel_watchdogs[info.udid] = _spawn_tunnel_watchdog(
                        info.udid,
                        expected_runner,
                        generation,
                        connection_lease=connected_lease,
                        connection_engine=connected_engine,
                    )
                    attempt.current_udid = info.udid
                    attempt.registry_key = info.udid
            if rearm_cancelled:
                raise TunnelStartCancelled
            await _stop_tunnel_parts(
                None,
                old_wd,
                side_effects=old_side_effects,
                caller="start_and_connect_rearm",
                udid=info.udid,
            )
        elif temp_key and temp_key != info.udid:
            old_wd = None
            old_side_effects = ()
            prior = None
            prior_wd = None
            prior_side_effects = ()
            runner = None
            rekey_cancelled = False
            async with _tunnels_lock:
                expected_runner = attempt.runner
                if (
                    _start_attempt_fenced_locked(attempt)
                    or expected_runner is None
                    or _tunnels.get(temp_key) is not expected_runner
                ):
                    rekey_cancelled = True
                else:
                    runner = _tunnels.pop(temp_key, None)
                    old_wd = _tunnel_watchdogs.pop(temp_key, None)
                    temp_generation = _tunnel_generations.get(temp_key)
                    old_side_effects = _take_tunnel_side_effects_locked(
                        temp_key,
                        generation=temp_generation,
                    )
                    if runner is not None:
                        _next_tunnel_generation_locked(temp_key)
                if not rekey_cancelled and runner is not None and runner.is_running():
                    # Replace any pre-existing entry under the real udid
                    # (defensive — shouldn't happen in normal flow).
                    prior = _tunnels.pop(info.udid, None)
                    prior_wd = _tunnel_watchdogs.pop(info.udid, None)
                    prior_generation = _tunnel_generations.get(info.udid)
                    prior_side_effects = _take_tunnel_side_effects_locked(
                        info.udid,
                        generation=prior_generation,
                    )
                    if prior is not None:
                        _next_tunnel_generation_locked(info.udid)
                    _tunnels[info.udid] = runner
                    generation = _next_tunnel_generation_locked(info.udid)
                    _tunnel_watchdogs[info.udid] = _spawn_tunnel_watchdog(
                        info.udid,
                        runner,
                        generation,
                        connection_lease=connected_lease,
                        connection_engine=connected_engine,
                    )
                    attempt.current_udid = info.udid
                    attempt.registry_key = info.udid
                elif not rekey_cancelled:
                    rekey_cancelled = True
            if rekey_cancelled:
                raise TunnelStartCancelled
            await _stop_tunnel_parts(
                None,
                old_wd,
                side_effects=old_side_effects,
                caller="start_and_connect_rekey",
                udid=temp_key,
            )
            if (
                prior is not None
                or prior_wd is not None
                or prior_side_effects
            ) and prior is not runner:
                await _stop_tunnel_parts(
                    prior,
                    prior_wd,
                    side_effects=prior_side_effects,
                    caller="start_and_connect_rekey",
                    udid=info.udid,
                )

        async with _tunnels_lock:
            if _start_attempt_fenced_locked(attempt):
                raise TunnelStartCancelled

        # The WebUI connects iOS 17+ devices through this composite WiFi
        # route, so mirror the USB watchdog's group-mode handoff here. Without
        # it a third phone is connected and has an engine, but remains idle
        # with no position while the primary keeps moving.
        try:
            from main import _auto_sync_new_device_to_primary
            await _auto_sync_new_device_to_primary(info.udid)
        except Exception:
            _tunnel_logger.exception(
                "Auto-sync of new WiFi device %s to primary failed",
                info.udid,
            )

        success = True
        return {
            "status": "connected",
            "udid": info.udid,
            "name": info.name,
            "ios_version": info.ios_version,
            "connection_type": "Network",
            "port": tunnel_result.get("port", req.port),
            "rsd_address": rsd_address,
            "rsd_port": rsd_port,
        }
    except TunnelStartTimedOut as e:
        await _cleanup_owned_start()
        raise HTTPException(
            status_code=500,
            detail={"code": "tunnel_timeout", "message": "Tunnel 啟動逾時"},
        ) from e
    except TunnelStartCancelled as e:
        await _cleanup_owned_start()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "tunnel_start_cancelled",
                "message": "Tunnel 啟動已被停止要求取消",
            },
        ) from e
    except asyncio.CancelledError:
        await _cleanup_owned_start()
        if attempt.cancel_requested:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "tunnel_start_cancelled",
                    "message": "Tunnel 啟動已被停止要求取消",
                },
            )
        raise
    except Exception as e:
        await _cleanup_owned_start()
        if isinstance(e, HTTPException):
            raise
        raise HTTPException(status_code=500, detail=f"Tunnel started but connection failed: {e}")
    finally:
        if adoption_lock_acquired:
            adoption_lock.release()
        await _finish_tunnel_start(attempt, success=success)


@router.post("/wifi/tunnel/start-and-connect")
async def wifi_tunnel_start_and_connect(req: WifiTunnelStartRequest):
    # The candidate lane wait is part of the end-to-end budget.  Adoption of
    # the resulting DM lease/engine happens later under the narrow commit lock.
    deadline = asyncio.get_running_loop().time() + TUNNEL_START_BUDGET
    attempt = await _register_tunnel_start(req.udid, deadline=deadline)
    entered = False
    try:
        async with _acquire_tunnel_start_lane_until(req.ip, deadline):
            entered = True
            return await _wifi_tunnel_start_and_connect_impl(
                req,
                attempt,
                lane_already_held=True,
            )
    except TunnelStartTimedOut as exc:
        if not entered:
            await _finish_tunnel_start(attempt, success=False)
        raise HTTPException(
            status_code=500,
            detail={"code": "tunnel_timeout", "message": "Tunnel 啟動逾時"},
        ) from exc
    except asyncio.CancelledError:
        if not entered:
            await _finish_tunnel_start(attempt, success=False)
        if attempt.cancel_requested:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "tunnel_start_cancelled",
                    "message": "Tunnel 啟動已被停止要求取消",
                },
            )
        raise


class WifiKeepaliveRequest(BaseModel):
    enabled: bool


@router.get("/wifi/tunnel/keepalive")
async def wifi_tunnel_keepalive_get():
    """Return whether the idle-tunnel keep-alive loop is enabled."""
    from main import app_state
    return {"enabled": bool(app_state._wifi_keepalive_enabled)}


@router.post("/wifi/tunnel/keepalive")
async def wifi_tunnel_keepalive_set(req: WifiKeepaliveRequest):
    """Enable / disable the keep-alive that re-pushes the current location
    to idle WiFi tunnels so iOS doesn't drop them when the screen is off."""
    from main import app_state
    app_state._wifi_keepalive_enabled = bool(req.enabled)
    try:
        app_state.save_settings()
    except Exception:
        pass  # best-effort persist; never fail the toggle
    return {"enabled": app_state._wifi_keepalive_enabled}


# ── Generic UDID routes (MUST be defined after all specific /wifi/* routes
#    so that /wifi/* paths do not accidentally match {udid}). ─────────────

@router.post("/{udid}/amfi/reveal-developer-mode")
async def amfi_reveal_developer_mode(udid: str):
    """Make iOS's "Developer Mode" option appear in Settings → Privacy &
    Security. Same end state as side-loading a developer-signed IPA via
    Sideloadly / Xcode, but done directly through AMFI so the user doesn't
    need a third-party side-loader. iOS 16+ only.

    This is action 0 (REVEAL) of the com.apple.amfi.lockdown service. It
    just creates the AMFIShowOverridePath marker file on the device —
    no reboot, no passcode prompt, completely safe. The user still has
    to open Settings and toggle Developer Mode on themselves (which iOS
    will then require passcode removal + reboot for, per Apple's rules).
    """
    dm = _dm()
    conn = dm._connections.get(udid)
    if conn is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "device_not_connected", "message": "裝置未連線,請先連線再試"},
        )

    # iOS 15 and below have no Developer Mode concept.
    try:
        major = int((conn.ios_version or "0.0").split(".")[0])
    except Exception:
        major = 0
    if major < 16:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "ios_too_old",
                "message": f"iOS {conn.ios_version} 沒有開發者模式,不需要此操作",
            },
        )

    try:
        from pymobiledevice3.services.amfi import AmfiService
    except ImportError as exc:
        raise HTTPException(
            status_code=500,
            detail={"code": "amfi_not_available", "message": f"AMFI 服務載入失敗: {exc}"},
        )

    # AMFI is a legacy lockdown service (com.apple.amfi.lockdown) that's only
    # advertised on the classic USB lockdown, NOT on iOS 17+'s RSD tunnel.
    # For iOS 17+ devices we stash the original USB lockdown on
    # conn.usbmux_lockdown; use it here. For iOS 16 devices conn.lockdown
    # IS the USB lockdown, so fall back to it.
    amfi_lockdown = getattr(conn, "usbmux_lockdown", None) or conn.lockdown
    if amfi_lockdown is None:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "amfi_needs_usb",
                "message": "AMFI 需要走 USB 連線。請插 USB 後再試(WiFi tunnel 不 advertise AMFI 服務)。",
            },
        )

    try:
        await AmfiService(amfi_lockdown).reveal_developer_mode_option_in_ui()
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "code": "amfi_reveal_failed",
                "message": f"AMFI reveal 失敗: {exc.__class__.__name__}: {exc}",
            },
        )

    return {"status": "ok"}


@router.post("/{udid}/connect")
async def connect_device(udid: str):
    from main import app_state
    from core.device_manager import UnsupportedIosVersionError
    dm = _dm()
    # Group-mode device cap. Allow re-connect of an already-connected udid.
    if udid not in dm._connections and len(dm._connections) >= MAX_DEVICES:
        raise HTTPException(
            status_code=409,
            detail={"code": "max_devices_reached", "message": f"已連接最多 {MAX_DEVICES} 台裝置"},
        )
    try:
        await dm.connect(udid)
        await app_state.create_engine_for_device(udid)
        try:
            from api.websocket import broadcast
            devs = await dm.discover_devices()
            info = next((d for d in devs if d.udid == udid), None)
            await broadcast("device_connected", {
                "udid": udid,
                "name": info.name if info else "",
                "ios_version": info.ios_version if info else "",
                "connection_type": info.connection_type if info else "USB",
            })
        except Exception:
            pass
        return {"status": "connected", "udid": udid}
    except UnsupportedIosVersionError as e:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "ios_unsupported",
                "message": (
                    f"偵測到 iOS {e.version},LocWarp 自 v0.1.49 起僅支援 "
                    f"iOS {UnsupportedIosVersionError.MIN_VERSION} 以上。"
                    f"請將裝置升級至 iOS {UnsupportedIosVersionError.MIN_VERSION} 或更新版本後再連線。"
                ),
                "ios_version": e.version,
                "min_version": UnsupportedIosVersionError.MIN_VERSION,
            },
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{udid}/connect")
async def disconnect_device(udid: str):
    from main import app_state
    dm = _dm()

    # WiFi devices: a bare dm.disconnect() closes the RSD but leaves the
    # tunnel runner + its watchdog armed. The watchdog then sees the socket
    # die, assumes a blip, and RESTARTS the tunnel — so the device the user
    # just disconnected pops back as "connected", and the restart churn
    # (engine rebuild + group auto-sync) can knock a sibling device into
    # device_lost. So for Network devices we must cancel the watchdog and
    # stop the runner, exactly like the Stop-Tunnel button (issue: right-
    # click disconnect on one device dropped all of them).
    conn = dm._connections.get(udid)
    is_network = conn is not None and getattr(conn, "connection_type", "") == "Network"
    has_tunnel = udid in _tunnels
    plan = await _request_tunnel_stop(udid, dm=dm)
    if is_network or has_tunnel or plan.parts or plan.matched_pending:
        cleaned = await _stop_tunnel_plan(plan, caller="user_disconnect")
        current = dm._connections.get(udid)
        current_engine = app_state.simulation_engines.get(udid)
        current_tunnel = udid in _tunnels
        if (
            not cleaned
            and (current is None or getattr(current, "connection_type", "") != "Network")
            and current_engine is None
            and not current_tunnel
        ):
            # Only pending/no-DM stops need a synthetic event.  If the exact
            # old lease lost a CAS race to C2, the replacement must remain
            # silent rather than receiving a stale disconnected broadcast.
            try:
                from api.websocket import broadcast
                await broadcast("device_disconnected", {"udid": udid, "udids": [udid], "reason": "user"})
            except Exception:
                pass
        return {"status": "disconnected", "udid": udid}

    # USB device: plain teardown.
    await dm.disconnect(udid)
    # Drop the per-udid engine (if any) so _engine() won't route to a dead service.
    app_state.simulation_engines.pop(udid, None)
    if app_state._primary_udid == udid:
        app_state._primary_udid = next(iter(app_state.simulation_engines), None)
    try:
        from api.websocket import broadcast
        await broadcast("device_disconnected", {"udid": udid, "udids": [udid], "reason": "user"})
    except Exception:
        pass
    return {"status": "disconnected", "udid": udid}


@router.get("/{udid}/info", response_model=DeviceInfo | None)
async def device_info(udid: str):
    dm = _dm()
    devices = await dm.discover_devices()
    for d in devices:
        if d.udid == udid:
            return d
    raise HTTPException(status_code=404, detail="Device not found")

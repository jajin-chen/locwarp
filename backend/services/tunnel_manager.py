"""Per-device WiFi tunnel lifecycle: registry, watchdog, restart/backoff.

Extracted from api/device.py (v0.2.9x) so that core/device_manager.py can
drive the same tunnel-restart path without importing the FastAPI router
(the old core→api import was a layering inversion papered over with
try/except ImportError). api/device.py keeps the routes and imports the
registry + helpers from here.
"""

import asyncio
import logging

from core.wifi_tunnel import TunnelRunner
from services.tunnel_discovery import find_fallback_endpoints

_tunnel_logger = logging.getLogger("wifi_tunnel")


def _dm():
    from main import app_state
    return app_state.device_manager


# Per-device tunnel registry. Each connected iOS 17+ device that uses
# WiFi (instead of USB) gets its own TunnelRunner. v0.2.83 lifted the
# previous singleton design so multiple iPhones can run on WiFi at once.
# Registry mutations go through _tunnels_lock; individual TunnelRunners
# also keep their own .lock for their own lifecycle (start/stop/wait).
_tunnels: dict[str, TunnelRunner] = {}
_tunnel_watchdogs: dict[str, asyncio.Task] = {}
_tunnels_lock = asyncio.Lock()


async def _cleanup_wifi_connection_for(udid: str, *, caller: str) -> bool:
    """Disconnect a single WiFi-connected device + drop its sim engine.
    Broadcasts device_disconnected for that udid so the frontend chip
    flips to disconnected. Returns True iff a Network connection was found
    and torn down.

    The engine MUST be stopped before the dm.disconnect closes the RSD —
    otherwise its in-flight task (random_walk / loop / multi_stop /
    navigate / _move_along_route) keeps trying to push positions through
    the now-dead RSD, hits DeviceLostError, retries, fails again, and
    floods the log with `Giving up on this route after repeated push
    failures` every ~2 seconds for as long as LocWarp stays open.
    Mirrors the USB watchdog teardown sequence in main.py:387-418."""
    from main import app_state
    dm = _dm()
    conn = dm._connections.get(udid)
    if conn is None or getattr(conn, "connection_type", "") != "Network":
        return False

    # Stop the running simulation BEFORE we close the underlying lockdown,
    # so its retry loop doesn't get a chance to log a dozen DeviceLostError
    # rounds against the dying RSD.
    old_eng = app_state.simulation_engines.get(udid)
    if old_eng is not None:
        try:
            from models.schemas import SimulationState as _SS
            old_eng.state = _SS.DISCONNECTED
            try:
                await old_eng._emit("state_change", {"state": old_eng.state.value})
            except Exception:
                _tunnel_logger.debug(
                    "[%s] disconnected state_change emit failed", caller, exc_info=True,
                )
            old_eng._stop_event.set()
            old_eng._pause_event.set()  # unstick anyone awaiting pause_event
            active = getattr(old_eng, "_active_task", None)
            if active is not None and not active.done():
                active.cancel()
        except Exception:
            _tunnel_logger.debug(
                "[%s] failed to stop old engine for %s", caller, udid, exc_info=True,
            )

    try:
        await dm.disconnect(udid)
        _tunnel_logger.info("[%s] Disconnected WiFi device %s", caller, udid)
    except (OSError, RuntimeError):
        _tunnel_logger.exception("[%s] Failed to disconnect %s", caller, udid)
    app_state.simulation_engines.pop(udid, None)
    if app_state._primary_udid == udid:
        remaining = next(iter(app_state.simulation_engines.keys()), None)
        app_state._primary_udid = remaining
    try:
        from api.websocket import broadcast
        await broadcast("device_disconnected", {
            "udid": udid,
            "udids": [udid],
            "reason": "wifi_tunnel_stopped",
            "remaining_count": len(dm._connections),
        })
    except Exception:
        _tunnel_logger.exception("[%s] WiFi cleanup broadcast failed", caller)
    return True


async def _cleanup_all_wifi_connections(caller: str = "unknown") -> list[str]:
    """Disconnect every Network device + drop their sim engines. Used by
    the legacy stop-all flow and shutdown paths."""
    import traceback
    dm = _dm()
    stack = traceback.extract_stack(limit=8)[:-1]
    stack_str = " <- ".join(f"{fr.name}@{fr.filename.split(chr(92))[-1]}:{fr.lineno}" for fr in reversed(stack))
    _tunnel_logger.warning(
        "_cleanup_all_wifi_connections called (caller=%s); stack: %s",
        caller, stack_str,
    )
    udids = [
        udid for udid, conn in list(dm._connections.items())
        if getattr(conn, "connection_type", "") == "Network"
    ]
    for udid in udids:
        await _cleanup_wifi_connection_for(udid, caller=caller)
    return udids


async def _tear_down_tunnel(udid: str, *, caller: str) -> None:
    """Cancel this udid's watchdog (if any) and stop the runner. Caller
    decides whether to also clean up the DM connection."""
    wd = _tunnel_watchdogs.pop(udid, None)
    if wd is not None and not wd.done():
        wd.cancel()
        try:
            await wd
        except (asyncio.CancelledError, Exception):
            pass
    runner = _tunnels.pop(udid, None)
    if runner is not None:
        try:
            await runner.stop()
        except Exception:
            _tunnel_logger.exception("[%s] runner.stop failed for %s", caller, udid)


# Restart backoff sequence (seconds). Three attempts cover most WiFi blips
# (transient packet loss, brief screen-lock pause) without sitting on a dead
# tunnel for an unbounded time. Total worst-case wait ~21s before final
# teardown — within the user's tolerance for "auto-recovers" before they'd
# look at the UI and notice.
_TUNNEL_RESTART_BACKOFF: tuple[float, ...] = (3.0, 6.0, 12.0)


async def _attempt_tunnel_restart(
    udid: str,
    ip: str,
    port: int,
    snapshot: dict | None,
    original_runner: TunnelRunner,
) -> bool:
    """Try one restart of the tunnel. On success, swaps in the new runner,
    rebuilds dm._connections + sim engine (since the new RSD interface gets
    a fresh address), and resumes any captured snapshot. Returns True on
    success, False otherwise. Caller decides whether to retry."""
    new_runner = TunnelRunner()
    try:
        info = await new_runner.start(udid, ip, port, timeout=10.0)
    except Exception as exc:
        _tunnel_logger.warning(
            "Tunnel restart failed for %s: %s: %s",
            udid, type(exc).__name__, exc,
        )
        return False

    new_rsd_address = info.get("rsd_address")
    new_rsd_port = info.get("rsd_port")
    if not new_rsd_address or not new_rsd_port:
        _tunnel_logger.warning(
            "Tunnel restart for %s returned no RSD info; treating as failure",
            udid,
        )
        try:
            await new_runner.stop()
        except Exception:
            pass
        return False

    from main import app_state
    try:
        async with _tunnels_lock:
            # User may have stopped this tunnel during our async window.
            if _tunnels.get(udid) is not original_runner:
                _tunnel_logger.info(
                    "Tunnel restart for %s racing user stop; discarding new runner",
                    udid,
                )
                try:
                    await new_runner.stop()
                except Exception:
                    pass
                # True here means "stop retrying" (racing user stop / a
                # concurrent restart already won), not "this candidate
                # succeeded". Both the direct-retry loop and the fallback
                # candidate loop in _per_tunnel_watchdog treat any truthy
                # return the same way: return immediately, don't try more
                # candidates.
                return True  # not really success, but caller should NOT retry
            _tunnels[udid] = new_runner

        # connect_wifi_tunnel internally calls disconnect(udid) if udid
        # already exists, so the old (now-dead) RSD lockdown gets torn
        # down correctly.
        dm = _dm()
        dev_info = await dm.connect_wifi_tunnel(new_rsd_address, new_rsd_port)

        # Discovery-assisted fallback can hand us an endpoint that turned
        # out to be a DIFFERENT iPhone (family device on the same LAN).
        # The pair-record handshake usually rejects that first, but if it
        # does connect, verify identity and bail — the except path below
        # rolls back the runner we registered. Compare case-insensitively:
        # dev_info.udid comes from the RSD peer_info while `udid` comes
        # from the pair-record filename / request, and those two sources
        # don't always agree on case (see main.py's connected_original /
        # present_usb_original normalization for the same issue). Skip the
        # check entirely for the "pending:ip:port" sentinel key used before
        # a real udid is known — it can never equal a real udid anyway.
        if not udid.startswith("pending:") and dev_info.udid.lower() != udid.lower():
            _tunnel_logger.warning(
                "Tunnel restart for %s reached a different device (%s); disconnecting",
                udid, dev_info.udid,
            )
            try:
                await dm.disconnect(dev_info.udid)
            except Exception:
                _tunnel_logger.debug(
                    "Disconnect of mismatched device failed", exc_info=True,
                )
            raise RuntimeError(
                f"udid mismatch: expected {udid}, got {dev_info.udid}"
            )

        # Rebuild the sim engine bound to the new location service. The
        # old engine pointed at the dead RSD and would throw
        # ConnectionTerminatedError on the next teleport / position push.
        app_state.simulation_engines.pop(dev_info.udid, None)
        if app_state._primary_udid == dev_info.udid:
            # Keep this udid as primary; create_engine_for_device only
            # promotes when _primary_udid is None.
            pass
        await app_state.create_engine_for_device(dev_info.udid)

        # Re-arm the watchdog on the new runner so subsequent blips get
        # the same recovery treatment.
        old_wd = _tunnel_watchdogs.pop(udid, None)
        if old_wd is not None and old_wd is not asyncio.current_task() and not old_wd.done():
            old_wd.cancel()
        _tunnel_watchdogs[udid] = asyncio.create_task(
            _per_tunnel_watchdog(udid, new_runner)
        )

        # Resume any in-flight simulation (navigate / loop / multi-stop /
        # random_walk) so the iPhone keeps moving instead of stopping at
        # the blip point. Snapshot only exists when this device was the
        # one driving the sim — followers don't capture one.
        if snapshot is not None:
            new_eng = app_state.simulation_engines.get(dev_info.udid)
            if new_eng is not None:
                _tunnel_logger.info(
                    "Resuming sim from snapshot after tunnel restart for %s (kind=%s)",
                    dev_info.udid, snapshot.get("kind"),
                )
                asyncio.create_task(new_eng.resume_from_snapshot(snapshot))
        else:
            # Group-mode: if this WiFi device was a follower of some other
            # primary (USB or WiFi), restart broke the follower task. Re-
            # arm the same teleport-to-primary + attach-as-follower flow
            # the USB watchdog uses for re-plugged USB devices, so dual /
            # triple-device groups stay locked together across a blip.
            try:
                from main import _auto_sync_new_device_to_primary
                asyncio.create_task(_auto_sync_new_device_to_primary(dev_info.udid))
            except Exception:
                _tunnel_logger.exception(
                    "Auto-sync after tunnel restart failed for %s", dev_info.udid,
                )

        try:
            from api.websocket import broadcast
            await broadcast("tunnel_recovered", {
                "udid": dev_info.udid,
                "rsd_address": new_rsd_address,
                "rsd_port": new_rsd_port,
                "ip": ip,
                "port": port,
            })
            await broadcast("device_connected", {
                "udid": dev_info.udid,
                "name": dev_info.name,
                "ios_version": dev_info.ios_version,
                "connection_type": "Network",
            })
        except Exception:
            _tunnel_logger.exception("Failed to broadcast tunnel_recovered for %s", udid)

        _tunnel_logger.info(
            "Tunnel restart succeeded for %s (rsd %s:%d)",
            udid, new_rsd_address, new_rsd_port,
        )
        return True
    except Exception:
        _tunnel_logger.exception(
            "Tunnel restart for %s started but post-setup failed; rolling back",
            udid,
        )
        # Roll back the new runner we registered, restoring the caller's
        # original runner rather than popping the slot empty. An empty
        # slot would make the "_tunnels.get(udid) is not runner" guards in
        # the watchdog's retry/fallback loops misread this as a user-
        # initiated stop and return early — skipping remaining fallback
        # candidates and the tunnel_lost teardown that's supposed to run
        # once every candidate is exhausted.
        async with _tunnels_lock:
            if _tunnels.get(udid) is new_runner:
                _tunnels[udid] = original_runner
        try:
            await new_runner.stop()
        except Exception:
            pass
        return False


async def _per_tunnel_watchdog(udid: str, runner: TunnelRunner) -> None:
    """Watch a single device's tunnel. If the runner's task dies (WiFi
    blip, iPhone locked, admin revoked), capture the sim state, then try
    up to len(_TUNNEL_RESTART_BACKOFF) restarts with backoff. Each restart
    rebuilds the device manager connection (the new TUN interface gets a
    fresh RSD address) and resumes the sim from snapshot so the iPhone
    keeps moving across the blip. Other tunnels stay isolated."""
    try:
        task = runner.task
        if task is None:
            return
        try:
            await task
        except asyncio.CancelledError:
            return
        except BaseException:
            pass

        # If the registry was already updated (explicit stop, re-key on
        # reconnect, etc.) this watchdog is stale.
        if _tunnels.get(udid) is not runner:
            return

        ip = runner.target_ip
        port = runner.target_port

        _tunnel_logger.warning(
            "Tunnel for %s exited unexpectedly (target=%s:%s); will attempt %d restart(s)",
            udid, ip, port, len(_TUNNEL_RESTART_BACKOFF),
        )
        try:
            from api.websocket import broadcast
            await broadcast("tunnel_degraded", {"udid": udid, "reason": "task_exited"})
        except Exception:
            _tunnel_logger.exception("Failed to emit tunnel_degraded event")

        if ip is None or port is None:
            # No target captured; we have nothing to retry against. Fall
            # through to teardown.
            _tunnel_logger.warning(
                "Tunnel for %s has no captured target ip/port; skipping retries",
                udid,
            )
        else:
            from main import app_state
            snapshot: dict | None = None
            old_eng = app_state.simulation_engines.get(udid)
            if old_eng is not None:
                try:
                    snapshot = old_eng.capture_resumable_snapshot()
                    if snapshot:
                        _tunnel_logger.info(
                            "Captured resumable snapshot for %s before tunnel restart (kind=%s)",
                            udid, snapshot.get("kind"),
                        )
                except Exception:
                    _tunnel_logger.exception("capture_resumable_snapshot failed for %s", udid)

                # Park the engine while we restart. Without this, multi-stop /
                # loop / random-walk keep iterating to the next leg, each call
                # burning ~3s in DvtLocationService._reconnect retries against
                # the dead RSD before raising DeviceLostError, then the handler
                # immediately tries the next leg. The log fills with "Giving up
                # on this route after repeated push failures" every ~6s for as
                # long as the watchdog is mid-restart. Cancelling the active
                # task here halts the thrash; on a successful restart, the
                # snapshot we just captured drives resume_from_snapshot back to
                # the same leg / segment.
                try:
                    from models.schemas import SimulationState as _SS
                    old_eng.state = _SS.DISCONNECTED
                    try:
                        await old_eng._emit("state_change", {"state": old_eng.state.value})
                    except Exception:
                        _tunnel_logger.debug(
                            "Disconnected state_change emit failed during watchdog pause",
                            exc_info=True,
                        )
                    old_eng._stop_event.set()
                    old_eng._pause_event.set()  # unstick anyone awaiting pause_event
                    active = getattr(old_eng, "_active_task", None)
                    if active is not None and not active.done():
                        active.cancel()
                except Exception:
                    _tunnel_logger.exception(
                        "Failed to park engine for %s before tunnel restart", udid,
                    )

            for attempt, delay in enumerate(_TUNNEL_RESTART_BACKOFF, start=1):
                try:
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    return

                # User may have explicitly stopped or replaced this tunnel
                # during the sleep; if so, abort the retry loop.
                if _tunnels.get(udid) is not runner:
                    _tunnel_logger.info(
                        "Tunnel for %s no longer registered (user stop?); aborting retries",
                        udid,
                    )
                    return

                _tunnel_logger.info(
                    "Tunnel restart attempt %d/%d for %s (after %.0fs backoff)",
                    attempt, len(_TUNNEL_RESTART_BACKOFF), udid, delay,
                )
                ok = await _attempt_tunnel_restart(udid, ip, port, snapshot, runner)
                if ok:
                    # On success the new watchdog has been armed and this
                    # one's job is done.
                    return

            # Direct retries against the old endpoint are exhausted. The
            # iPhone likely rebound its RemotePairing port (reboot / WiFi
            # rejoin) or got a new DHCP lease — re-discover before giving
            # up. Each phase runs once; total added time is bounded by
            # one port scan + one discover pass.
            candidates: list[tuple[str, int]] = []
            try:
                candidates = await find_fallback_endpoints(ip)
            except Exception:
                _tunnel_logger.exception(
                    "Fallback endpoint discovery failed for %s", udid,
                )
            if len(candidates) > 8:
                # Bound worst-case time: each candidate attempt can take
                # several seconds, so an unbounded list turns one bad blip
                # into a long thrash. 8 was picked as a generous cap for a
                # home/office LAN scan, not a measured limit.
                _tunnel_logger.info(
                    "Fallback discovery returned %d candidates for %s; trying first 8",
                    len(candidates), udid,
                )
                candidates = candidates[:8]
            for cand_ip, cand_port in candidates:
                if _tunnels.get(udid) is not runner:
                    _tunnel_logger.info(
                        "Tunnel for %s no longer registered during fallback; aborting",
                        udid,
                    )
                    return
                if cand_ip == ip and cand_port == port:
                    continue  # the direct-retry loop already tried this exact endpoint
                if any(
                    r is not runner and r.target_ip == cand_ip and r.target_port == cand_port
                    for r in _tunnels.values()
                ):
                    # This endpoint is already claimed by another live
                    # tunnel (e.g. a second, currently-connected iPhone on
                    # the same LAN). Starting a new tunnel against it would
                    # tear down that device's active connection via
                    # connect_wifi_tunnel's implicit disconnect(udid).
                    _tunnel_logger.info(
                        "Skipping fallback candidate %s:%d for %s; claimed by another tunnel",
                        cand_ip, cand_port, udid,
                    )
                    continue
                _tunnel_logger.info(
                    "Fallback restart attempt for %s via %s:%d",
                    udid, cand_ip, cand_port,
                )
                ok = await _attempt_tunnel_restart(
                    udid, cand_ip, cand_port, snapshot, runner,
                )
                if ok:
                    return

        # All retries exhausted (or no target to retry against).
        _tunnel_logger.warning(
            "Tunnel for %s could not be restarted; tearing down WiFi connection",
            udid,
        )
        async with _tunnels_lock:
            current = _tunnels.get(udid)
            if current is runner:
                _tunnels.pop(udid, None)
            wd = _tunnel_watchdogs.pop(udid, None)
            if wd is not None and wd is not asyncio.current_task() and not wd.done():
                wd.cancel()
            await _cleanup_wifi_connection_for(udid, caller="watchdog_tunnel_died")
            try:
                from api.websocket import broadcast
                await broadcast("tunnel_lost", {"udid": udid, "reason": "task_exited"})
            except Exception:
                _tunnel_logger.exception("Failed to emit tunnel_lost event")
    except asyncio.CancelledError:
        raise
    except Exception:
        # Anything unhandled here (e.g. an exception leaking out of
        # _attempt_tunnel_restart) would otherwise die silently as an
        # unobserved task exception — log it so a crashed watchdog is at
        # least visible instead of just going quiet.
        _tunnel_logger.exception("Watchdog for %s crashed unexpectedly", udid)

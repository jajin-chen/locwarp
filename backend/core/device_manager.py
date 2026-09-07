"""
LocWarp Device Manager

Handles iOS device detection, connection lifecycle, tunnel establishment,
and location service creation.  Wraps pymobiledevice3 internals so the
rest of the application never touches low-level device APIs directly.

Supports both USB and WiFi connections.  ``list_devices()`` from usbmuxd
returns devices with ``connection_type`` of ``"USB"`` or ``"Network"``.
WiFi requires the device to be paired and on the same local network.

For iOS 17+, a TCP tunnel via CoreDeviceTunnelProxy is established first,
then a RemoteServiceDiscoveryService (RSD) is created over the tunnel to
access DVT services.  This requires administrator privileges on Windows.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Dict, Optional

from pymobiledevice3.lockdown import create_using_usbmux, create_using_tcp
from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
from pymobiledevice3.remote.tunnel_service import CoreDeviceTunnelProxy
from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation
from pymobiledevice3.services.simulate_location import DtSimulateLocation
from pymobiledevice3.usbmux import list_devices

from config import DEVICE_NAMES_FILE
from models.schemas import DeviceInfo
from services.json_safe import safe_load_json, safe_write_json
from services.location_service import (
    DeviceLostError,
    DvtLocationService,
    LegacyLocationService,
    LocationService,
)


class UnsupportedIosVersionError(RuntimeError):
    """Raised when a connecting device's iOS version is below the minimum
    supported by LocWarp (currently 16.0). Surfaces a structured error to
    the API layer so the frontend can show an actionable message rather
    than a stack trace."""

    MIN_VERSION = "16.0"

    def __init__(self, version: str) -> None:
        self.version = version
        super().__init__(f"iOS {version} is not supported (requires {self.MIN_VERSION}+)")

logger = logging.getLogger(__name__)


# The detached-close policy is carried in the cleanup task's context rather
# than in the private method signature.  A few lightweight DeviceManager test
# doubles (and older integrations) override ``_close_detached_connection``
# with the original two-argument contract; keeping that contract avoids
# turning a lifecycle hardening change into an adapter break.
_clear_location_on_detached_close = contextvars.ContextVar(
    "clear_location_on_detached_close",
    default=True,
)


class UsbmuxAvailability:
    """Debounce usbmuxd connection failures.

    On machines without Apple Mobile Device Service every USB poll fails;
    without this, discover_devices logs a full ERROR traceback every few
    seconds forever. State machine: first failure logs once and pauses
    usbmux attempts for `cooldown` seconds; recovery logs once.
    """

    def __init__(self, cooldown: float = 60.0) -> None:
        self.cooldown = cooldown
        self._down_until: float = 0.0
        self._was_down = False

    def should_attempt(self, now: float) -> bool:
        return now >= self._down_until

    def record_failure(self, now: float) -> bool:
        """Register a failed attempt. Returns True iff this is a fresh
        outage (caller should log it)."""
        self._down_until = now + self.cooldown
        fresh = not self._was_down
        self._was_down = True
        return fresh

    def record_success(self) -> bool:
        """Register a successful attempt. Returns True iff the service
        just recovered from an outage."""
        recovered = self._was_down
        self._was_down = False
        self._down_until = 0.0
        return recovered


usbmux_availability = UsbmuxAvailability()


def _parse_ios_version(version_string: str) -> tuple[int, ...]:
    """Convert an iOS version string like '17.4.1' into a comparable tuple."""
    try:
        return tuple(int(p) for p in version_string.split("."))
    except (ValueError, AttributeError):
        logger.warning("Unable to parse iOS version '%s', assuming 0.0", version_string)
        return (0, 0)


def _load_device_name_cache() -> Dict[str, str]:
    """Load the persisted UDID → DeviceName map. Returns empty dict on any failure."""
    raw = safe_load_json(DEVICE_NAMES_FILE)
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items() if isinstance(v, str) and v}


def _remember_device_name(udid: str, name: str) -> None:
    """Persist a real DeviceName for *udid* if it isn't a generic fallback.

    The cache only stores user-set names. We deliberately skip the
    DeviceClass fallback ("iPhone") and "Unknown" so a once-known real
    name isn't overwritten by a later degraded read.
    """
    if not udid or not name:
        return
    if name in ("iPhone", "iPad", "iPod touch", "Unknown"):
        return
    cache = _load_device_name_cache()
    if cache.get(udid) == name:
        return
    cache[udid] = name
    safe_write_json(DEVICE_NAMES_FILE, cache)


@dataclass
class _ActiveConnection:
    """Internal bookkeeping for a single connected device."""
    udid: str
    lockdown: object  # LockdownClient or RemoteServiceDiscoveryService
    ios_version: str
    connection_type: str = "USB"  # "USB" or "Network"
    name: str = "iPhone"  # Cached DeviceName so discover_devices can surface
                          # WiFi-tunnel devices that no longer appear in usbmuxd
                          # after USB is unplugged (RemotePairing tunnel only).
    dvt_provider: Optional[DvtProvider] = None
    tunnel_proxy: Optional[CoreDeviceTunnelProxy] = None
    tunnel_context: object = None  # async context manager for the tunnel
    rsd: Optional[RemoteServiceDiscoveryService] = None
    location_service: Optional[LocationService] = None
    usbmux_lockdown: object = None  # Original lockdown client (for legacy fallback on iOS 17+)
    location_init_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False, compare=False)


class DeviceManager:
    """
    Manages the full lifecycle of iOS device connections.

    Usage::

        dm = DeviceManager()
        devices = await dm.discover_devices()
        await dm.connect(devices[0].udid)
        loc = await dm.get_location_service(devices[0].udid)
        await loc.set(37.7749, -122.4194)
        await dm.disconnect(devices[0].udid)
    """

    def __init__(self) -> None:
        self._connections: Dict[str, _ActiveConnection] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    async def discover_devices(self) -> list[DeviceInfo]:
        """
        Scan for all iOS devices visible over USB and WiFi (usbmuxd).

        usbmuxd returns both USB-connected and WiFi-paired devices on
        the same network.  Each device carries a ``connection_type`` of
        ``"USB"`` or ``"Network"``.

        Returns a list of ``DeviceInfo`` objects with basic identification
        data.  This does **not** establish a persistent connection.
        """
        devices: list[DeviceInfo] = []
        seen_udids: set[str] = set()

        now = time.monotonic()
        raw_devices = []
        if usbmux_availability.should_attempt(now):
            try:
                raw_devices = await list_devices()
                if usbmux_availability.record_success():
                    logger.info("usbmuxd reachable again — USB discovery resumed")
            except Exception as exc:
                if usbmux_availability.record_failure(time.monotonic()):
                    logger.warning(
                        "usbmuxd unreachable (%s: %s) — USB discovery paused, "
                        "retrying every %.0fs. Is Apple Mobile Device Service "
                        "(iTunes / Apple Devices) installed and running?",
                        type(exc).__name__, exc, usbmux_availability.cooldown,
                    )

        for raw in raw_devices:
            try:
                conn_type = getattr(raw, "connection_type", "USB")
                # If we already saw this device via USB, skip the Network duplicate
                if raw.serial in seen_udids:
                    # But upgrade to USB if this entry is USB (prefer USB info)
                    if conn_type == "USB":
                        for d in devices:
                            if d.udid == raw.serial:
                                d.connection_type = "USB"
                    continue
                seen_udids.add(raw.serial)

                lockdown = await create_using_usbmux(serial=raw.serial)
                all_values = lockdown.all_values
                # If device is already connected, report the active connection type
                active_conn = self._connections.get(raw.serial)
                if active_conn:
                    conn_type = active_conn.connection_type
                device_name = all_values.get("DeviceName", "Unknown")
                _remember_device_name(raw.serial, device_name)
                info = DeviceInfo(
                    udid=raw.serial,
                    name=device_name,
                    ios_version=all_values.get("ProductVersion", "0.0"),
                    connection_type=conn_type,
                )
                info.is_connected = raw.serial in self._connections
                # Query Developer Mode status (iOS 16+). Tolerate failure —
                # None means "unknown", frontend will hide the reveal button.
                try:
                    ver = _parse_ios_version(info.ios_version)
                    if ver >= (16, 0):
                        info.developer_mode_enabled = await lockdown.get_developer_mode_status()
                except Exception:
                    logger.debug("get_developer_mode_status failed for %s", raw.serial, exc_info=True)
                devices.append(info)
                logger.debug("Discovered device %s (%s) running iOS %s via %s (connected=%s)",
                             info.name, info.udid, info.ios_version, conn_type, info.is_connected)
            except Exception:
                logger.exception("Failed to query device %s", getattr(raw, "serial", "?"))

        # Surface devices that are in our connection table but did not get
        # added from usbmuxd above. Happens for the dual-device A-WiFi +
        # B-USB flow: A is paired via the in-process RemotePairing tunnel
        # (port 49152), NOT through usbmuxd's iTunes-WiFi-sync path, so
        # once A's USB cable is unplugged usbmuxd may stop listing A
        # entirely. Without this fallback `discover_devices()` would
        # return only B, and the frontend's listDevices refresh on B's
        # auto-connect broadcast would wipe A out of the device sidebar /
        # connectedDevices fanout, so the user would see A as if it had
        # been kicked. Compare against actually-added udids (not
        # `seen_udids` which is set early for raw-entry dedup) so a
        # failed lockdown query above doesn't suppress the fallback.
        added_udids = {d.udid for d in devices}
        for udid, conn in self._connections.items():
            if udid in added_udids:
                continue
            try:
                info = DeviceInfo(
                    udid=udid,
                    name=conn.name or "iPhone",
                    ios_version=conn.ios_version or "0.0",
                    connection_type=conn.connection_type or "Network",
                )
                info.is_connected = True
                devices.append(info)
                logger.debug(
                    "Discovered cached %s device %s (%s) iOS %s (no usbmux entry)",
                    conn.connection_type, info.name, udid, info.ios_version,
                )
            except Exception:
                logger.exception("Failed to surface cached connection for %s", udid)

        return devices

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    async def connect(self, udid: str) -> None:
        """
        Establish a connection appropriate for the device's iOS version.

        Supports both USB and WiFi (Network) connections via usbmuxd.

        * **iOS 17+** -- TCP tunnel via CoreDeviceTunnelProxy + RSD.
        * **iOS 16.x** -- plain lockdown over usbmux + legacy location service.
        """
        async with self._lock:
            if udid in self._connections:
                logger.info("Device %s is already connected", udid)
                return

        # Detect connection type from usbmux device list.
        connection_type = "USB"
        try:
            raw_devices = await list_devices()
            for raw in raw_devices:
                if raw.serial == udid:
                    connection_type = getattr(raw, "connection_type", "USB")
                    # Prefer USB if device shows up as both
                    if connection_type == "USB":
                        break
        except Exception:
            logger.debug("Could not determine connection type for %s, assuming USB", udid)

        logger.info("Connecting to %s via %s", udid, connection_type)

        # Create a fresh lockdown client to read the iOS version.
        try:
            lockdown = await create_using_usbmux(serial=udid)
        except Exception:
            logger.exception("Cannot create lockdown client for %s via %s", udid, connection_type)
            raise

        ios_version_str: str = lockdown.all_values.get("ProductVersion", "0.0")
        device_name: str = lockdown.all_values.get("DeviceName", "iPhone")
        _remember_device_name(udid, device_name)
        ver = _parse_ios_version(ios_version_str)

        if ver < (16, 0):
            logger.warning(
                "Refusing connect: %s reports iOS %s, below minimum %s",
                udid, ios_version_str, UnsupportedIosVersionError.MIN_VERSION,
            )
            raise UnsupportedIosVersionError(ios_version_str)

        if ver >= (17, 0):
            conn = await self._connect_tunnel(udid, lockdown, ios_version_str)
        else:
            conn = self._connect_legacy(udid, lockdown, ios_version_str)
        conn.connection_type = connection_type
        conn.name = device_name

        async with self._lock:
            self._connections[udid] = conn

        logger.info("Connected to %s (iOS %s) via %s", udid, ios_version_str, connection_type)

    # -- iOS 17+ via CoreDeviceTunnelProxy ---------------------------------

    async def _connect_tunnel(
        self, udid: str, lockdown, ios_version: str
    ) -> _ActiveConnection:
        """TCP tunnel for iOS 17+ using CoreDeviceTunnelProxy + RSD."""
        logger.debug("Establishing TCP tunnel for %s (iOS %s)", udid, ios_version)

        try:
            proxy = await CoreDeviceTunnelProxy.create(lockdown)
            tunnel_ctx = proxy.start_tcp_tunnel()
            tunnel_result = await tunnel_ctx.__aenter__()

            logger.info("Tunnel established for %s: %s:%s",
                        udid, tunnel_result.address, tunnel_result.port)

            # Create RSD over the tunnel
            rsd = RemoteServiceDiscoveryService((tunnel_result.address, tunnel_result.port))
            await rsd.connect()
            logger.info("RSD connected for %s", udid)

            return _ActiveConnection(
                udid=udid,
                lockdown=rsd,
                ios_version=ios_version,
                tunnel_proxy=proxy,
                tunnel_context=tunnel_ctx,
                rsd=rsd,
                usbmux_lockdown=lockdown,
            )
        except Exception:
            logger.exception(
                "TCP tunnel failed for %s (iOS %s). "
                "Ensure you are running as administrator.",
                udid, ios_version,
            )
            raise RuntimeError(
                f"無法建立裝置通道 (iOS {ios_version})。"
                f"請以系統管理員身份執行 LocWarp。"
            )

    # iOS < 17 path removed in v0.1.49 — see UnsupportedIosVersionError.

    def _connect_legacy(
        self, udid: str, lockdown, ios_version: str
    ) -> _ActiveConnection:
        """Direct usbmux lockdown connection for iOS 16.x devices."""
        logger.info("Using legacy lockdown connection for %s (iOS %s)", udid, ios_version)
        return _ActiveConnection(
            udid=udid,
            lockdown=lockdown,
            ios_version=ios_version,
            usbmux_lockdown=lockdown,
        )

    # ------------------------------------------------------------------
    # Disconnection
    # ------------------------------------------------------------------

    async def _detach_connection(
        self,
        udid: str,
        *,
        expected: _ActiveConnection | None = None,
    ) -> _ActiveConnection | None:
        """Atomically remove *udid* and return its owned connection.

        ``expected`` is an identity lease, not a value comparison.  A stale
        cleanup therefore has no side effects when a newer connection has
        already replaced the old one.
        """
        async with self._lock:
            current = self._connections.get(udid)
            if expected is not None and current is not expected:
                return None
            return self._connections.pop(udid, None)

    async def _close_detached_connection(
        self,
        udid: str,
        conn: _ActiveConnection,
    ) -> None:
        """Close resources from a previously detached connection.

        Location clearing is disabled by the cleanup task context when a
        connection is being replaced.
        The old location service may be backed by a dead DVT channel; calling
        its normal ``clear()`` in that state can use its UDID-based reconnect
        factory and accidentally acquire a provider on the new connection.
        Replacement callers stop the old simulation engine before this
        method, then close the exact old provider/RSD lease without allowing
        cross-generation recovery.  Ordinary user disconnects retain the
        best-effort clear behavior.
        """
        # Clear any active location simulation first, but never reconnect a
        # retiring lease after a replacement has already been installed.
        if (
            _clear_location_on_detached_close.get()
            and conn.location_service is not None
        ):
            try:
                await conn.location_service.clear()
            except Exception:
                logger.exception("Error clearing location on disconnect for %s", udid)

        # Shut down the DVT provider if it was opened.
        if conn.dvt_provider is not None:
            try:
                await conn.dvt_provider.__aexit__(None, None, None)
            except Exception:
                logger.exception("Error closing DvtProvider for %s", udid)

        # Close RSD.
        if conn.rsd is not None:
            try:
                await conn.rsd.close()
            except Exception:
                logger.exception("Error closing RSD for %s", udid)

        # Close tunnel context.
        if conn.tunnel_context is not None:
            try:
                await conn.tunnel_context.__aexit__(None, None, None)
            except Exception:
                logger.exception("Error closing tunnel for %s", udid)

        # Close tunnel proxy.
        if conn.tunnel_proxy is not None:
            try:
                conn.tunnel_proxy.close()
            except Exception:
                logger.exception("Error closing tunnel proxy for %s", udid)

        logger.info("Disconnected device %s", udid)

    async def _drain_cleanup(self, awaitable, *, label: str) -> bool:
        """Drain one cleanup task despite caller cancellation."""
        cleanup = asyncio.create_task(awaitable)
        caller_cancelled = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                caller_cancelled = True

        try:
            cleanup.result()
        except BaseException as exc:
            if not caller_cancelled:
                raise
            logger.debug(
                "Cleanup failed while caller was cancelling %s: %s",
                label,
                exc,
                exc_info=True,
            )
        return caller_cancelled

    async def _drain_detached_close(
        self,
        udid: str,
        conn: _ActiveConnection,
        *,
        clear_location: bool = True,
    ) -> bool:
        """Finish a detached close even when the caller is cancelled."""
        token = _clear_location_on_detached_close.set(clear_location)
        try:
            cleanup = self._close_detached_connection(udid, conn)
            return await self._drain_cleanup(
                cleanup,
                label=f"detached connection {udid}",
            )
        finally:
            _clear_location_on_detached_close.reset(token)

    async def _abort_previous_connection(
        self,
        udid: str,
        previous: _ActiveConnection | None,
    ) -> None:
        """Detach and drain a predecessor after pre-close adoption aborts.

        The pre-close callback may have already stopped the predecessor's
        engine.  Leaving that exact lease in ``_connections`` would therefore
        expose a current connection whose engine is gone.  Keep the cleanup
        conditional on the predecessor's identity so a concurrent replacement
        remains untouched, and shield the whole detach/close transaction from
        any cancellation that triggered the abort.
        """
        if previous is None:
            return

        async def _detach_and_close() -> None:
            detached = await self._detach_connection(udid, expected=previous)
            if detached is None:
                return
            await self._close_detached_connection(udid, detached)

            # A newer lease may have won while the old resources were being
            # drained.  Re-check after close so an adoption-abort event can
            # never make a healthy replacement appear disconnected.  Keep
            # the manager lock through the event: this rare abort path is
            # deliberately serialized against a USB C2 install so the
            # no-replacement decision and its notification stay one event.
            async with self._lock:
                if self._connections.get(udid) is not None:
                    return
                remaining_count = len(self._connections)
                try:
                    from api.websocket import broadcast

                    await broadcast("device_disconnected", {
                        "udid": udid,
                        "udids": [udid],
                        "reason": "wifi_tunnel_adoption_aborted",
                        "remaining_count": remaining_count,
                    })
                except Exception:
                    logger.exception(
                        "Failed to broadcast WiFi adoption abort for %s",
                        udid,
                    )

        try:
            await self._drain_cleanup(
                _detach_and_close(),
                label=f"aborted previous WiFi connection {udid}",
            )
        except BaseException:
            # Preserve the callback's cancellation/error as the primary
            # failure while still recording an unusual cleanup failure.
            logger.exception(
                "Failed to abort previous WiFi connection %s after "
                "pre-close callback failure",
                udid,
            )

    async def _drain_rsd_close(
        self,
        rsd: RemoteServiceDiscoveryService,
    ) -> bool:
        """Close an RSD created before a Network lease was installed."""
        try:
            return await self._drain_cleanup(
                rsd.close(),
                label="uninstalled WiFi RSD",
            )
        except (OSError, ConnectionError):
            # Preserve the existing retry behavior for a close that is
            # already disconnected; cancellation has still been drained by
            # ``_drain_cleanup`` before this handler runs.
            return False

    async def disconnect(
        self,
        udid: str,
        *,
        expected: _ActiveConnection | None = None,
    ) -> None:
        """Detach and close *udid*; optionally require an exact lease."""
        conn = await self._detach_connection(udid, expected=expected)
        if conn is None:
            if expected is None:
                logger.warning("Disconnect requested for unknown device %s", udid)
            else:
                logger.warning("Disconnect requested for unknown or replaced device %s", udid)
            return
        cancelled = await self._drain_detached_close(udid, conn)
        if cancelled:
            raise asyncio.CancelledError

    async def disconnect_if_current(
        self,
        udid: str,
        expected: _ActiveConnection,
    ) -> bool:
        """Close only when *expected* is still the active lease."""
        conn = await self._detach_connection(udid, expected=expected)
        if conn is None:
            return False
        cancelled = await self._drain_detached_close(udid, conn)
        if cancelled:
            raise asyncio.CancelledError
        return True

    # ------------------------------------------------------------------
    # Location service
    # ------------------------------------------------------------------

    async def get_location_service(self, udid: str) -> LocationService:
        """
        Return a ``LocationService`` instance for the given device.

        The concrete type depends on the iOS version:

        * iOS 17+  ->  ``DvtLocationService`` (uses DVT instrumentation)
        * iOS < 17 ->  ``LegacyLocationService`` (uses DtSimulateLocation)

        The service is cached on the connection so subsequent calls are cheap.
        """
        async with self._lock:
            conn = self._connections.get(udid)

        if conn is None:
            raise RuntimeError(
                f"Device {udid} is not connected. Call connect() first."
            )

        async with conn.location_init_lock:
            async with self._lock:
                if self._connections.get(udid) is not conn:
                    raise DeviceLostError(f"Device {udid} connection lease was replaced")
                if conn.location_service is not None:
                    return conn.location_service

            previous_dvt = conn.dvt_provider
            try:
                ver = _parse_ios_version(conn.ios_version)
                if ver >= (17, 0):
                    loc = await self._create_dvt_location_service(conn)
                else:
                    loc = await self._create_legacy_location_service(conn)
                async with self._lock:
                    if self._connections.get(udid) is not conn:
                        raise DeviceLostError(f"Device {udid} connection lease was replaced")
                    conn.location_service = loc
                    return loc
            except BaseException:
                # Initialization may finish after detached cleanup already ran.
                # Close only the provider acquired here, never the replacement.
                orphan = conn.dvt_provider
                if orphan is not None and orphan is not previous_dvt:
                    conn.dvt_provider = None
                    try:
                        await self._drain_cleanup(
                            orphan.__aexit__(None, None, None),
                            label=f"aborted location initialization {udid}",
                        )
                    except Exception:
                        logger.exception("Failed closing aborted DVT initialization for %s", udid)
                raise

    async def _ensure_personalized_ddi_mounted(self, conn: _ActiveConnection) -> None:
        """Check whether the Personalized DDI is mounted on the iPhone.

        v0.2.58 change: LocWarp no longer auto-downloads / auto-mounts
        the DDI. On iOS 26.4.1 the 20MB image upload routinely dropped
        the RSD tunnel mid-transfer, poisoning subsequent DVT calls
        with InvalidService. We now rely on the iPhone already having
        the DDI mounted (Xcode, 3uTools, 愛思助手, pymobiledevice3 CLI,
        or an earlier successful mount that iOS is still caching).

        This method is therefore a pure status check. If the iPhone
        has DDI mounted we log it and return happily. If not, we emit
        a WS event so the UI can tell the user to mount it via another
        tool, and we return anyway — the caller (`_create_dvt_location_service`)
        will then attempt DVT directly and produce a clean error if
        dtservicehub isn't advertised.
        """
        try:
            from pymobiledevice3.services.mobile_image_mounter import MobileImageMounterService
        except ImportError as exc:
            logger.warning(
                "pymobiledevice3 mobile_image_mounter not importable (%s: %s); "
                "skipping DDI status check", type(exc).__name__, exc,
            )
            return

        mounted = False
        try:
            mounter = MobileImageMounterService(lockdown=conn.lockdown)
            try:
                await mounter.connect()
                mounted = await mounter.is_image_mounted("Personalized")
            finally:
                try:
                    await mounter.close()
                except Exception:
                    pass
        except Exception:
            logger.warning("Could not query DDI mount status on %s", conn.udid, exc_info=True)
            return

        if mounted:
            logger.info("Personalized DDI already mounted on %s; DVT should work", conn.udid)
            try:
                from api.websocket import broadcast
                await broadcast("ddi_mounted", {"udid": conn.udid})
            except Exception:
                pass
            return

        logger.info(
            "Image mounter did not report a Personalized DDI on %s; "
            "trying DVT directly. If location service setup fails, "
            "check the device's DDI mount state.", conn.udid,
        )
        try:
            from api.websocket import broadcast
            await broadcast("ddi_not_mounted", {
                "udid": conn.udid,
                "hint": (
                    "影像服務未回報已掛載 DDI,LocWarp 仍會嘗試 DVT 定位服務。"
                    "若定位連線失敗,請確認這支 iPhone 的 DDI 掛載狀態後再重新連接。"
                ),
            })
        except Exception:
            pass

    async def _ensure_classic_ddi_mounted(self, conn: _ActiveConnection) -> None:
        """Best-effort Developer Disk Image mount for iOS 16.x devices."""
        try:
            import pymobiledevice3.services.mobile_image_mounter as mim
        except ImportError as exc:
            logger.warning(
                "mobile_image_mounter not importable for classic DDI (%s: %s); "
                "skipping classic DDI mount",
                type(exc).__name__, exc,
            )
            return

        mounter_cls = getattr(mim, "MobileImageMounterService", None)
        if mounter_cls is not None:
            try:
                mounter = mounter_cls(lockdown=conn.lockdown)
                try:
                    await mounter.connect()
                    if await mounter.is_image_mounted("Developer"):
                        logger.debug("Classic DDI already mounted on %s", conn.udid)
                        return
                finally:
                    try:
                        await mounter.close()
                    except Exception:
                        pass
            except Exception:
                logger.warning("Could not query classic DDI mount state", exc_info=True)

        mount_fn = None
        for name in ("auto_mount_developer", "auto_mount", "auto_mount_disk_image"):
            candidate = getattr(mim, name, None)
            if callable(candidate):
                mount_fn = candidate
                break
        if mount_fn is None:
            logger.warning("No classic DDI auto-mount helper found; continuing without mount")
            return

        logger.info("Classic DDI not mounted on %s; attempting auto-mount", conn.udid)
        try:
            from api.websocket import broadcast
            await broadcast("ddi_mounting", {"udid": conn.udid})
        except Exception:
            pass

        mounted = False
        try:
            await asyncio.wait_for(mount_fn(conn.lockdown), timeout=120.0)
            mounted = True
            logger.info("Classic DDI mounted successfully for %s", conn.udid)
        except Exception:
            logger.warning("Classic DDI auto-mount failed for %s", conn.udid, exc_info=True)
        finally:
            try:
                from api.websocket import broadcast
                event = "ddi_mounted" if mounted else "ddi_mount_failed"
                payload = {"udid": conn.udid}
                if not mounted:
                    payload["error"] = "Classic DDI mount failed"
                await broadcast(event, payload)
            except Exception:
                pass

    async def _create_dvt_location_service(
        self, conn: _ActiveConnection
    ) -> DvtLocationService:
        """Spin up a DVT provider and hand it to ``DvtLocationService``.

        Check the reported DDI state, then try DVT directly. Personalized
        images are not automatically mounted here.
        """
        # Check DDI status without changing the device's mounted images.
        try:
            await self._ensure_personalized_ddi_mounted(conn)
        except Exception:
            logger.warning("DDI status check failed; trying DVT directly", exc_info=True)

        try:
            dvt = DvtProvider(conn.lockdown)
            try:
                # pymobiledevice3 publishes its DTX handle only after the
                # handshake and does not clean up CancelledError mid-enter.
                # Drain that bounded handshake before closing on cancellation.
                cancelled = await self._drain_cleanup(
                    dvt.__aenter__(), label=f"DVT handshake {conn.udid}",
                )
                if cancelled:
                    raise asyncio.CancelledError
            except BaseException:
                # A cancelled/failed handshake can already own a socket even
                # though it has not yet been published on the connection.
                try:
                    await self._drain_cleanup(
                        dvt.__aexit__(None, None, None),
                        label=f"failed DVT handshake {conn.udid}",
                    )
                except Exception:
                    logger.exception("Failed closing DVT handshake for %s", conn.udid)
                raise
            conn.dvt_provider = dvt
            logger.debug("DVT provider opened for %s", conn.udid)
            # Bind a per-udid factory so DvtLocationService._reconnect can
            # ask us for a fresh DvtProvider on the *current* lockdown.
            # This is what makes the location service survive WiFi tunnel
            # restarts — when the tunnel watchdog rebuilds the tunnel and
            # replaces conn.lockdown, the factory picks up the new one
            # automatically instead of rebuilding on a now-orphan ref.
            udid = conn.udid

            async def _factory(_udid: str = udid) -> DvtProvider:
                # Bind recovery to this exact connection lease.  A retiring
                # location service must never reacquire a provider on a newer
                # WiFi connection that happens to use the same UDID.
                return await self.get_fresh_dvt_provider(_udid, expected=conn)

            return DvtLocationService(
                dvt,
                lockdown=conn.lockdown,
                dvt_factory=_factory,
                udid=udid,
            )
        except Exception as dvt_exc:
            legacy_lockdown = conn.usbmux_lockdown or conn.lockdown
            if isinstance(legacy_lockdown, RemoteServiceDiscoveryService):
                # RSD does not advertise the legacy lockdown developer service.
                # Its lazy constructor would appear to succeed, then fail every
                # location update and leave an unusable service cached.
                logger.warning(
                    "DVT location service failed for %s (%s); no direct lockdown "
                    "connection is available for the legacy fallback",
                    conn.udid, dvt_exc,
                )
                raise
            logger.warning(
                "DVT location service failed for %s (%s). Falling back to "
                "legacy DtSimulateLocation over lockdown.",
                conn.udid, dvt_exc,
            )
            # iOS 17+ still exposes com.apple.dt.simulatelocation on some
            # devices (reported working on iOS 26 by multiple users), so
            # try the legacy service before giving up entirely.
            try:
                # Verify availability without changing the device's location.
                # DtSimulateLocation opens a new connection for every command,
                # so constructing the wrapper alone cannot validate this fallback.
                probe = await legacy_lockdown.start_lockdown_developer_service(
                    DtSimulateLocation.SERVICE_NAME
                )
                await probe.close()
                legacy = LegacyLocationService(legacy_lockdown)
                logger.info("Using LegacyLocationService fallback for %s", conn.udid)
                return legacy
            except Exception:
                logger.exception(
                    "Both DVT and legacy location services failed for %s", conn.udid
                )
                raise dvt_exc

    async def _create_legacy_location_service(
        self, conn: _ActiveConnection
    ) -> LegacyLocationService:
        """Build the legacy location service for iOS 16.x devices."""
        try:
            await self._ensure_classic_ddi_mounted(conn)
        except Exception:
            logger.warning("Classic DDI auto-mount failed; legacy location may still fail", exc_info=True)
        logger.info("Using LegacyLocationService for %s", conn.udid)
        return LegacyLocationService(conn.lockdown)

    # _ensure_classic_ddi_mounted, _create_legacy_location_service, and
    # connect_wifi (legacy direct-IP WiFi) removed in v0.1.49 — see
    # UnsupportedIosVersionError. iOS 17+ continues to use the
    # personalized DDI mount path + DvtLocationService (with
    # LegacyLocationService as a runtime fallback inside
    # _create_dvt_location_service when DVT itself fails).

    # ------------------------------------------------------------------
    # WiFi connection (iOS 17+ tunnel only)
    # ------------------------------------------------------------------

    async def connect_wifi_tunnel_owned(
        self,
        rsd_address: str,
        rsd_port: int,
        *,
        before_close_previous: Callable[
            [str, _ActiveConnection | None], Awaitable[None]
        ] | None = None,
    ) -> tuple[DeviceInfo, _ActiveConnection]:
        """Connect to a device via an existing WiFi tunnel.

        Use this when a WiFi tunnel has already been established (by the
        in-process ``TunnelRunner`` or ``pymobiledevice3 remote start-tunnel``).
        The caller provides the RSD address and port.

        Returns a ``DeviceInfo`` and the exact connection lease installed for
        it.  The lease can later be passed to ``disconnect(expected=...)`` so
        stale rollback cannot remove a newer connection for the same UDID.
        """
        logger.info("Connecting via WiFi tunnel RSD at %s:%d", rsd_address, rsd_port)

        import asyncio as _asyncio
        rsd = None
        last_exc: Exception | None = None
        # TUN interface routes may take a few seconds to become reachable
        # after the tunnel process reports ready, so retry with backoff.
        for attempt in range(1, 11):
            rsd = RemoteServiceDiscoveryService((rsd_address, rsd_port))
            try:
                await rsd.connect()
                last_exc = None
                break
            except asyncio.CancelledError:
                # ``CancelledError`` is a BaseException and bypasses the
                # retry handler below.  The RSD may nevertheless have
                # partially opened its transport, so drain it before the
                # cancellation escapes this method.
                await self._drain_rsd_close(rsd)
                rsd = None
                raise
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "RSD connect attempt %d/10 failed (%s): %s",
                    attempt, exc.__class__.__name__, exc,
                )
                close_cancelled = await self._drain_rsd_close(rsd)
                rsd = None
                if close_cancelled:
                    raise asyncio.CancelledError
                await _asyncio.sleep(min(0.5 * attempt, 2.0))
            except BaseException:
                # System-level BaseExceptions have the same ownership rule as
                # cancellation: close the partially connected RSD, then
                # re-raise without retrying.
                await self._drain_rsd_close(rsd)
                rsd = None
                raise

        if last_exc is not None:
            logger.error("Failed to connect to RSD at %s:%d after retries", rsd_address, rsd_port)
            raise RuntimeError(
                f"無法連線到 WiFi tunnel RSD ({rsd_address}:{rsd_port})。"
                "請確認 WiFi tunnel 仍然活躍。"
            ) from last_exc

        installed = False
        try:
            peer = rsd.peer_info or {}
            props = peer.get("Properties", {})
            udid = props.get("UniqueDeviceID", "")
            ios_version_str = props.get("OSVersion", "0.0")
            # peer_info["Properties"] only carries DeviceClass ("iPhone"), not
            # the user-set DeviceName (e.g. "My iPhone"). RSD.connect() already
            # opens a lockdown service over the tunnel internally and exposes
            # the result as rsd.all_values, so the live DeviceName is right
            # there for free. We still keep two fallbacks for the edge case
            # where the lockdown sub-service failed (e.g. RemoteXPC variants
            # that don't advertise it): a still-active USB conn's cached name,
            # then the persisted ~/.locwarp/device_names.json populated
            # whenever USB or discovery saw a real DeviceName.
            all_values = getattr(rsd, "all_values", None) or {}
            device_name = all_values.get("DeviceName") or ""
            if not device_name:
                existing = self._connections.get(udid)
                if existing is not None and existing.name and existing.name != "iPhone":
                    device_name = existing.name
            if not device_name:
                cached = _load_device_name_cache().get(udid)
                if cached:
                    device_name = cached
            if not device_name:
                device_name = props.get("DeviceClass", "iPhone")
            # Live DeviceName from the WiFi tunnel is just as authoritative as
            # USB, so feed it back into the persistent cache too — covers the
            # "user renamed the device since last USB plug" case.
            _remember_device_name(udid, device_name)

            conn = _ActiveConnection(
                udid=udid,
                lockdown=rsd,
                ios_version=ios_version_str,
                connection_type="Network",
                name=device_name,
                rsd=rsd,
            )

            # Snapshot the predecessor without installing C1.  Production
            # adopters use the callback to quiesce the exact old engine while
            # C0 remains current/open; the callback is deliberately outside
            # the manager lock so its drain cannot block connection lookups.
            async with self._lock:
                previous = self._connections.get(udid)
            if before_close_previous is not None:
                try:
                    callback_cancelled = await self._drain_cleanup(
                        before_close_previous(udid, previous),
                        label=f"before-close WiFi connection {udid}",
                    )
                except BaseException:
                    await self._abort_previous_connection(udid, previous)
                    raise
                if callback_cancelled:
                    await self._abort_previous_connection(udid, previous)
                    raise asyncio.CancelledError
                # A concurrent reconnect may have replaced C0 while the
                # pre-close callback was draining its exact E0.  Never install
                # C1 over that newer lease or close it as our predecessor.
                try:
                    await self._lock.acquire()
                except BaseException:
                    # The callback has already quiesced E0 while C0 remains
                    # current.  If lock acquisition is cancelled or fails,
                    # abort that exact predecessor before propagating.
                    await self._abort_previous_connection(udid, previous)
                    raise
                try:
                    if self._connections.get(udid) is not previous:
                        raise RuntimeError(
                            f"WiFi connection adoption conflict for {udid}"
                        )
                    self._connections[udid] = conn
                    installed = True
                finally:
                    self._lock.release()
            else:
                # Compatibility path for legacy callers: preserve the
                # original atomic swap semantics when no pre-close callback is
                # supplied.
                async with self._lock:
                    previous = self._connections.get(udid)
                    self._connections[udid] = conn
                installed = True
            if previous is not None and previous is not conn:
                # C0 has already been superseded by C1.  Do not let C0's
                # location service reconnect through the UDID-based factory
                # and allocate an extra DVT provider on C1.
                cancelled = await self._drain_detached_close(
                    udid,
                    previous,
                    clear_location=False,
                )
                if cancelled:
                    # The lease was installed but never delivered to the caller.
                    # Roll back only our exact object; a concurrent reconnect C2
                    # must remain the active owner and must not be closed here.
                    own = await self._detach_connection(udid, expected=conn)
                    if own is not None:
                        await self._drain_detached_close(udid, own)
                    raise asyncio.CancelledError

            logger.info("WiFi tunnel connected to %s (iOS %s)", udid, ios_version_str)

            return DeviceInfo(
                udid=udid,
                name=device_name,
                ios_version=ios_version_str,
                connection_type="Network",
                is_connected=True,
            ), conn
        except BaseException:
            if not installed and rsd is not None:
                try:
                    await self._drain_rsd_close(rsd)
                except BaseException:
                    logger.exception("Failed to close uninstalled WiFi RSD")
            raise

    async def connect_wifi_tunnel(
        self, rsd_address: str, rsd_port: int
    ) -> DeviceInfo:
        """Compatibility wrapper returning only ``DeviceInfo``."""
        info, _lease = await self.connect_wifi_tunnel_owned(rsd_address, rsd_port)
        return info

    async def scan_wifi_devices(
        self,
        subnet: str | None = None,
        timeout: float = 0.5,
    ) -> list[dict]:
        """Scan the local network for iOS devices on port 62078 (lockdownd).

        Tries each IP in the subnet concurrently.  Returns a list of
        ``{"ip": ..., "name": ..., "udid": ...}`` dicts for reachable
        devices.

        If *subnet* is not given, the local machine's subnet is guessed
        from the default route interface.
        """
        if subnet is None:
            subnet = _guess_local_subnet()
            if subnet is None:
                logger.warning("Cannot determine local subnet for WiFi scan")
                return []

        logger.info("Scanning subnet %s for iOS devices...", subnet)

        # Generate IPs: e.g. "192.168.1" → .1 to .254
        base = subnet.rsplit(".", 1)[0]
        ips = [f"{base}.{i}" for i in range(1, 255)]

        async def _probe(ip: str) -> dict | None:
            try:
                _, writer = await asyncio.wait_for(
                    asyncio.open_connection(ip, 62078),
                    timeout=timeout,
                )
                writer.close()
                await writer.wait_closed()
                # Port is open — try a quick lockdown to get device info
                try:
                    pair_rec = _load_pair_record()
                    lockdown = await asyncio.wait_for(
                        create_using_tcp(
                            ip,
                            pair_record=pair_rec,
                            autopair=pair_rec is None,
                        ),
                        timeout=5.0,
                    )
                    vals = lockdown.all_values
                    return {
                        "ip": ip,
                        "name": vals.get("DeviceName", "Unknown"),
                        "udid": vals.get("UniqueDeviceID", lockdown.udid or ""),
                        "ios_version": vals.get("ProductVersion", "0.0"),
                    }
                except Exception:
                    # Port open but lockdown failed — still report it
                    return {"ip": ip, "name": "iOS Device", "udid": "", "ios_version": ""}
            except (OSError, asyncio.TimeoutError):
                return None

        results = await asyncio.gather(*[_probe(ip) for ip in ips])
        found = [r for r in results if r is not None]
        logger.info("WiFi scan found %d device(s)", len(found))
        return found

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    @property
    def connected_udids(self) -> list[str]:
        """Return the UDIDs of all currently connected devices."""
        return list(self._connections.keys())

    def is_connected(self, udid: str) -> bool:
        """Check whether a device is currently connected."""
        return udid in self._connections

    def get_connection_type(self, udid: str) -> str:
        """Return ``'USB'`` or ``'Network'`` for a connected device."""
        conn = self._connections.get(udid)
        return conn.connection_type if conn else "USB"

    # ------------------------------------------------------------------
    # Recovery helpers (used by location_service factory + API safety net)
    # ------------------------------------------------------------------

    async def get_fresh_dvt_provider(
        self,
        udid: str,
        *,
        timeout: float = 15.0,
        expected: _ActiveConnection | None = None,
    ) -> DvtProvider:
        """Return a freshly-opened ``DvtProvider`` for *udid*.

        Used by ``DvtLocationService._reconnect`` after the DVT instrument
        channel drops. Probes connection health, transparently waits for
        any in-flight WiFi tunnel restart driven by ``_per_tunnel_watchdog``
        (see ``services/tunnel_manager.py``), then opens a new ``DvtProvider`` on the
        *current* lockdown. The previous provider stored on the active
        connection is closed best-effort.

        Raises ``DeviceLostError`` (with a categorised ``reason``) when
        no live provider can be obtained inside *timeout* seconds —
        typically because the user really did unplug USB, turn off the
        iPhone, or the WiFi tunnel cannot be restarted.
        """
        import time
        deadline = time.monotonic() + timeout
        last_exc: Exception | None = None

        while True:
            async with self._lock:
                conn = self._connections.get(udid)

            if expected is not None and conn is not expected:
                raise DeviceLostError(
                    f"Device {udid} connection lease was replaced",
                    reason=DeviceLostError.REASON_TUNNEL_DEAD,
                )

            if conn is None:
                raise DeviceLostError(
                    f"Device {udid} no longer connected",
                    reason=DeviceLostError.REASON_USB_GONE,
                )

            # WiFi: peek at the tunnel runner. If it has died, the watchdog
            # is in the middle of restarting it — wait until either a fresh
            # runner appears (success path swaps in a new TunnelRunner and
            # replaces conn.lockdown along the way) or we time out.
            if conn.connection_type == "Network":
                from services.tunnel_manager import _tunnels
                runner = _tunnels.get(udid)
                if runner is not None and not runner.is_running():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise DeviceLostError(
                            f"WiFi tunnel for {udid} did not restart in {timeout:.0f}s",
                            reason=DeviceLostError.REASON_TUNNEL_DEAD,
                        )
                    await asyncio.sleep(min(0.5, remaining))
                    continue

            # USB, or WiFi with a live tunnel: try opening a new DvtProvider.
            try:
                new_dvt = DvtProvider(conn.lockdown)
                await new_dvt.__aenter__()
            except Exception as exc:
                last_exc = exc
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.warning(
                        "get_fresh_dvt_provider exhausted for %s: %s", udid, exc,
                    )
                    raise DeviceLostError(
                        f"Could not open DvtProvider for {udid}: {exc}",
                        reason=DeviceLostError.REASON_LOCKDOWN_DEAD,
                    ) from exc
                await asyncio.sleep(min(0.5, remaining))
                continue

            # Success — swap into the active connection record so future
            # discover/clear paths find it. Revalidate the exact lease after
            # the await: a tunnel restart may have replaced this connection
            # while the provider handshake was in flight.
            async with self._lock:
                lease_current = self._connections.get(udid) is conn and (
                    expected is None or conn is expected
                )
                if lease_current:
                    old_dvt = conn.dvt_provider
                    conn.dvt_provider = new_dvt
                else:
                    old_dvt = None
            if not lease_current:
                try:
                    await new_dvt.__aexit__(None, None, None)
                except Exception:
                    logger.debug(
                        "Ignoring error closing provider opened for stale lease %s",
                        udid,
                        exc_info=True,
                    )
                raise DeviceLostError(
                    f"Device {udid} connection lease was replaced",
                    reason=DeviceLostError.REASON_TUNNEL_DEAD,
                )
            if old_dvt is not None and old_dvt is not new_dvt:
                try:
                    await old_dvt.__aexit__(None, None, None)
                except Exception:
                    logger.debug(
                        "Ignoring error closing stale DvtProvider for %s",
                        udid, exc_info=True,
                    )
            logger.info("DVT provider re-acquired for %s", udid)
            return new_dvt

    async def full_reconnect(
        self, udid: str, *, expected: _ActiveConnection | None = None,
    ) -> bool:
        """Last-resort recovery: force a complete teardown + reconnect.

        Used as the API-layer safety net (``api/location.py``) when the
        location service's factory-driven reconnect still raised
        ``DeviceLostError``. For WiFi this drives the same restart path
        the tunnel watchdog uses (rebuilding tunnel + RSD lockdown +
        DvtProvider). For USB, this disconnects + reconnects from
        scratch.

        Returns ``True`` when *udid* is healthily connected at exit.
        """
        async with self._lock:
            conn = self._connections.get(udid)
            if expected is not None and conn is not expected:
                logger.info("full_reconnect: skipping retired connection for %s", udid)
                return False
        conn_type = conn.connection_type if conn else None

        from services.tunnel_manager import _tunnels, _attempt_tunnel_restart

        if _should_use_wifi_recovery(conn_type, _tunnels, udid):
            runner = _tunnels.get(udid)
            if runner is None or not runner.target_ip or not runner.target_port:
                logger.debug(
                    "full_reconnect: no live tunnel runner for %s; cannot recover", udid,
                )
                return False
            try:
                if expected is not None:
                    ok = await _attempt_tunnel_restart(
                        udid, runner.target_ip, runner.target_port, None, runner,
                        connection_lease=expected,
                    )
                else:
                    ok = await _attempt_tunnel_restart(
                        udid, runner.target_ip, runner.target_port, None, runner,
                    )
                return bool(ok)
            except Exception:
                logger.exception("full_reconnect: WiFi tunnel restart failed for %s", udid)
                return False

        # USB (or unknown type — try the bluntest recovery available).
        try:
            try:
                if expected is not None:
                    if not await self.disconnect_if_current(udid, expected):
                        return False
                else:
                    await self.disconnect(udid)
            except Exception:
                logger.debug("full_reconnect: USB disconnect failed", exc_info=True)
            await self.connect(udid)
            async with self._lock:
                return udid in self._connections
        except Exception:
            logger.exception("full_reconnect: USB reconnect failed for %s", udid)
            return False

    async def disconnect_all(self) -> None:
        """Disconnect every active device."""
        udids = list(self._connections.keys())
        for udid in udids:
            await self.disconnect(udid)
        logger.info("All devices disconnected")


def _should_use_wifi_recovery(
    conn_type: str | None, tunnels_map: dict, udid: str,
) -> bool:
    """Decide whether ``full_reconnect`` should take the WiFi tunnel-restart
    path for *udid* instead of the blunt USB disconnect/reconnect fallback.

    ``conn_type`` is ``None`` whenever the connection record has already
    been removed — the most common case, since ``full_reconnect`` is
    invoked as the API-layer safety net after a ``DeviceLostError`` has
    already triggered cleanup. In that case, fall back to checking whether
    a WiFi tunnel runner still exists for *udid*: its presence means the
    device was WiFi-connected, so it should still get the tunnel-restart
    path rather than the USB fallback, which would first disconnect any
    recoverable state and then attempt a connection method that cannot
    succeed on a machine without usbmuxd.

    Note this only decides routing; the WiFi path re-validates the runner
    (target IP/port) before using it, so an incomplete runner entry still
    falls back to reporting failure rather than silently using USB.
    """
    if conn_type == "Network":
        return True
    if conn_type is None:
        return tunnels_map.get(udid) is not None
    return False


def _load_pair_record(udid: str | None = None) -> dict | None:
    """Load a USB pair record from Apple's system Lockdown store.

    On Windows, pair records live in ``%ALLUSERSPROFILE%\\Apple\\Lockdown``.
    If *udid* is given, loads that specific record; otherwise loads the
    first ``.plist`` found (most setups have only one device).
    """
    import os
    import plistlib

    lockdown_dir = Path(os.environ.get("ALLUSERSPROFILE", "C:/ProgramData")) / "Apple" / "Lockdown"
    if not lockdown_dir.exists():
        logger.debug("Apple Lockdown directory not found: %s", lockdown_dir)
        return None

    target: Path | None = None
    if udid:
        candidate = lockdown_dir / f"{udid}.plist"
        if candidate.exists():
            target = candidate
    else:
        # Pick the first device plist (skip SystemConfiguration.plist)
        for f in lockdown_dir.glob("*.plist"):
            if f.stem != "SystemConfiguration":
                target = f
                break

    if target is None:
        logger.debug("No pair record found in %s", lockdown_dir)
        return None

    try:
        with open(target, "rb") as fh:
            record = plistlib.load(fh)
        logger.debug("Loaded pair record from %s", target)
        return record
    except Exception:
        logger.exception("Failed to load pair record from %s", target)
        return None


def _guess_local_subnet() -> str | None:
    """Best-effort guess of the local LAN subnet (e.g. '192.168.1.0/24').

    Returns the base IP like '192.168.1.0' or ``None`` if unable to determine.
    """
    try:
        # Open a UDP socket to a public IP (doesn't actually send)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        # Return the /24 base
        parts = local_ip.rsplit(".", 1)
        return f"{parts[0]}.0"
    except (OSError, IndexError):
        return None

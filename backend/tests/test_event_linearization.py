"""Deterministic lifecycle-event linearization regressions.

Each test holds the event emitter at the exact point where an old lifecycle is
being announced, then starts the next owner.  The next owner may wait for the
transaction boundary, but it must never commit before the old event has
finished.  These are intentionally barrier-driven tests rather than sleeps so
they remain stable on both the default and Windows Selector event loops.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import api.websocket as websocket
import core.device_manager as device_manager_module
from core.device_manager import DeviceManager, _ActiveConnection
import services.tunnel_manager as tunnel_manager


class _ObservedLock:
    """An asyncio lock that exposes blocked acquire attempts to a test."""

    def __init__(self) -> None:
        self._inner = asyncio.Lock()
        self.watch_attempts = False
        self.acquire_attempted = asyncio.Event()

    def arm_attempt_observer(self) -> None:
        self.watch_attempts = True
        self.acquire_attempted.clear()

    def locked(self) -> bool:
        return self._inner.locked()

    async def acquire(self) -> bool:
        if self.watch_attempts:
            self.acquire_attempted.set()
        return await self._inner.acquire()

    def release(self) -> None:
        self._inner.release()

    async def __aenter__(self) -> "_ObservedLock":
        await self.acquire()
        return self

    async def __aexit__(self, *_args) -> None:
        self.release()


class _TraceConnections(dict[str, object]):
    """Record a replacement commit after the initial old lease is seeded."""

    def __init__(self, trace: list[str]) -> None:
        super().__init__()
        self.trace = trace
        self.record_replacements = False

    def __setitem__(self, key: str, value: object) -> None:
        super().__setitem__(key, value)
        if self.record_replacements:
            self.trace.append("C2.commit")


class _CallbackRsd:
    instances: list["_CallbackRsd"] = []

    def __init__(self, address: tuple[str, int]) -> None:
        self.address = address
        self.peer_info = {
            "Properties": {
                "UniqueDeviceID": "udid-linearization",
                "OSVersion": "17.5",
                "DeviceClass": "iPhone",
            }
        }
        self.all_values = {"DeviceName": "Linearization phone"}
        self.close_calls = 0
        self.instances.append(self)

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        self.close_calls += 1


def _patch_callback_rsd(monkeypatch: pytest.MonkeyPatch) -> None:
    _CallbackRsd.instances.clear()
    monkeypatch.setattr(
        device_manager_module,
        "RemoteServiceDiscoveryService",
        _CallbackRsd,
    )
    monkeypatch.setattr(
        device_manager_module,
        "_remember_device_name",
        lambda *_args: None,
    )


def _connection(name: str) -> _ActiveConnection:
    return _ActiveConnection(
        udid="udid-linearization",
        lockdown=object(),
        ios_version="17.5",
        connection_type="Network",
        name=name,
    )


class _Engine:
    def __init__(self, name: str) -> None:
        self.name = name
        self.state = None
        self._stop_event = asyncio.Event()
        self._pause_event = asyncio.Event()
        self._active_task: asyncio.Task | None = None


class _LockedLeaseDeviceManager:
    """Small DM double with the same lock boundary used by production code."""

    def __init__(self, udid: str, trace: list[str]) -> None:
        self.udid = udid
        self._lock = _ObservedLock()
        self._connections: _TraceConnections = _TraceConnections(trace)
        self.trace = trace
        self.connect_attempted = asyncio.Event()
        self.close_calls: list[tuple[str, object]] = []
        self.connect_count = 0

    async def connect_wifi_tunnel_owned(
        self,
        _address: str,
        _port: int,
        before_close_previous=None,
        **kwargs,
    ) -> tuple[SimpleNamespace, object]:
        callback = before_close_previous
        if callback is None:
            callback = next(
                (value for value in kwargs.values() if callable(value)),
                None,
            )
        previous = self._connections.get(self.udid)
        if callback is not None:
            result = callback(self.udid, previous)
            if asyncio.iscoroutine(result):
                await result

        self.connect_attempted.set()
        async with self._lock:
            self.connect_count += 1
            connection = SimpleNamespace(
                name=f"C{self.connect_count}",
                connection_type="Network",
            )
            self._connections[self.udid] = connection
        return (
            SimpleNamespace(
                udid=self.udid,
                name="Linearization phone",
                ios_version="17.5",
            ),
            connection,
        )

    async def _detach_connection(
        self,
        udid: str,
        *,
        expected: object | None = None,
    ) -> object | None:
        async with self._lock:
            current = self._connections.get(udid)
            if expected is not None and current is not expected:
                return None
            return self._connections.pop(udid, None)

    async def _close_detached_connection(self, udid: str, conn: object) -> None:
        self.close_calls.append((udid, conn))

    async def install_usb_c2(self) -> None:
        """Model the USB watchdog's competing DM lease installation."""
        self.connect_attempted.set()
        async with self._lock:
            self.trace.append("USB C2.commit")
            self._connections[self.udid] = SimpleNamespace(
                name="USB-C2",
                connection_type="USB",
            )


class _ExitedRunner:
    def __init__(self) -> None:
        self.task = asyncio.create_task(asyncio.sleep(0))
        self.target_ip: str | None = None
        self.target_port: int | None = None
        self.stop_calls = 0

    async def stop(self) -> None:
        self.stop_calls += 1
        await asyncio.gather(self.task, return_exceptions=True)


async def test_device_manager_abort_event_precedes_c2_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An adoption-abort event is complete before a waiting C2 can install."""

    _patch_callback_rsd(monkeypatch)
    manager = DeviceManager()
    udid = "udid-linearization"
    old = _connection("C0")
    trace: list[str] = []
    connections = _TraceConnections(trace)
    connections[udid] = old
    manager._connections = connections  # type: ignore[assignment]
    observed_lock = _ObservedLock()
    manager._lock = observed_lock  # type: ignore[assignment]

    broadcast_started = asyncio.Event()
    release_broadcast = asyncio.Event()

    async def broadcast(event_type: str, _payload: dict) -> None:
        if event_type != "device_disconnected":
            return
        trace.append("old.device_disconnected.enter")
        broadcast_started.set()
        await release_broadcast.wait()
        trace.append("old.device_disconnected.exit")

    monkeypatch.setattr(websocket, "broadcast", broadcast)

    async def fail_before_close(*_args) -> None:
        raise RuntimeError("abort C1")

    c1 = asyncio.create_task(
        manager.connect_wifi_tunnel_owned(
            "192.0.2.10",
            49152,
            before_close_previous=fail_before_close,
        )
    )
    c2: asyncio.Task | None = None
    try:
        await asyncio.wait_for(broadcast_started.wait(), timeout=0.5)
        # The old event is the transaction boundary.  A C2 install must not
        # acquire this lock while that event is still in flight.
        assert observed_lock.locked()
        observed_lock.arm_attempt_observer()
        connections.record_replacements = True
        c2 = asyncio.create_task(
            manager.connect_wifi_tunnel_owned("192.0.2.11", 49153)
        )
        await asyncio.wait_for(observed_lock.acquire_attempted.wait(), timeout=0.5)
        await asyncio.sleep(0)
        assert not c2.done()
        assert "C2.commit" not in trace

        release_broadcast.set()
        with pytest.raises(RuntimeError, match="abort C1"):
            await asyncio.wait_for(c1, timeout=0.5)
        await asyncio.wait_for(c2, timeout=0.5)
    finally:
        release_broadcast.set()
        if not c1.done():
            c1.cancel()
        if c2 is not None and not c2.done():
            c2.cancel()
        await asyncio.gather(c1, *( [c2] if c2 is not None else [] ), return_exceptions=True)
        if observed_lock.locked():
            observed_lock.release()

    assert trace == [
        "old.device_disconnected.enter",
        "old.device_disconnected.exit",
        "C2.commit",
    ]
    assert manager._connections[udid] is not old
    assert _CallbackRsd.instances[-1].close_calls == 0


async def test_cleanup_disconnect_event_precedes_c2_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Normal WiFi cleanup linearizes its disconnect event before DM C2."""

    import main

    udid = "udid-cleanup-linearization"
    trace: list[str] = []
    dm = _LockedLeaseDeviceManager(udid, trace)
    old = SimpleNamespace(name="C0", connection_type="Network")
    dm._connections[udid] = old
    dm._connections.record_replacements = False
    monkeypatch.setattr(tunnel_manager, "_dm", lambda: dm)
    monkeypatch.setattr(
        main.app_state,
        "simulation_engines",
        {udid: _Engine("E0")},
    )
    monkeypatch.setattr(main.app_state, "_primary_udid", udid)

    broadcast_started = asyncio.Event()
    release_broadcast = asyncio.Event()

    async def broadcast(event_type: str, _payload: dict) -> None:
        if event_type != "device_disconnected":
            return
        trace.append("old.device_disconnected.enter")
        broadcast_started.set()
        # The registry lock must not be held while the websocket transport is
        # awaited; only the DM lifecycle boundary may block a replacement.
        assert not tunnel_manager._tunnels_lock.locked()
        await release_broadcast.wait()
        trace.append("old.device_disconnected.exit")

    monkeypatch.setattr(websocket, "broadcast", broadcast)

    cleanup = asyncio.create_task(
        tunnel_manager._cleanup_wifi_connection_for(
            udid,
            caller="test_cleanup_event_linearization",
            expected_connection=old,
            expected_engine=main.app_state.simulation_engines[udid],
        )
    )
    c2: asyncio.Task | None = None
    try:
        await asyncio.wait_for(broadcast_started.wait(), timeout=0.5)
        assert dm._lock.locked()
        dm._lock.arm_attempt_observer()
        dm._connections.record_replacements = True
        c2 = asyncio.create_task(
            dm.connect_wifi_tunnel_owned("192.0.2.12", 49154)
        )
        await asyncio.wait_for(dm._lock.acquire_attempted.wait(), timeout=0.5)
        await asyncio.sleep(0)
        assert not c2.done()
        assert "C2.commit" not in trace

        release_broadcast.set()
        assert await asyncio.wait_for(cleanup, timeout=0.5) is True
        await asyncio.wait_for(c2, timeout=0.5)
    finally:
        release_broadcast.set()
        if not cleanup.done():
            cleanup.cancel()
        if c2 is not None and not c2.done():
            c2.cancel()
        await asyncio.gather(
            cleanup,
            *( [c2] if c2 is not None else [] ),
            return_exceptions=True,
        )
        if dm._lock.locked():
            dm._lock.release()

    assert trace == [
        "old.device_disconnected.enter",
        "old.device_disconnected.exit",
        "C2.commit",
    ]


async def test_watchdog_loss_event_precedes_wifi_and_usb_replacements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """tunnel_lost holds lifecycle/DM boundaries but not ``_tunnels_lock``."""

    import main

    udid = "udid-watchdog-linearization"
    trace: list[str] = []
    dm = _LockedLeaseDeviceManager(udid, trace)
    old = SimpleNamespace(name="C0", connection_type="Network")
    dm._connections[udid] = old
    monkeypatch.setattr(tunnel_manager, "_dm", lambda: dm)

    old_engine = _Engine("E0")
    monkeypatch.setattr(main.app_state, "simulation_engines", {udid: old_engine})
    monkeypatch.setattr(main.app_state, "_primary_udid", udid)
    monkeypatch.setattr(tunnel_manager, "_TUNNEL_RESTART_BACKOFF", ())

    loss_started = asyncio.Event()
    release_loss = asyncio.Event()

    async def broadcast(event_type: str, _payload: dict) -> None:
        if event_type == "device_disconnected":
            trace.append("device_disconnected")
            return
        if event_type != "tunnel_lost":
            return
        trace.append("tunnel_lost.enter")
        loss_started.set()
        # The registry lock protects ownership snapshots only.  It must be
        # released before the awaitable websocket broadcast.
        assert not tunnel_manager._tunnels_lock.locked()
        assert dm._lock.locked()
        await release_loss.wait()
        trace.append("tunnel_lost.exit")

    monkeypatch.setattr(websocket, "broadcast", broadcast)

    runner = _ExitedRunner()
    tunnel_manager._tunnels[udid] = runner  # type: ignore[assignment]
    tunnel_manager._tunnel_generations[udid] = 1
    old_watchdog = asyncio.create_task(
        tunnel_manager._per_tunnel_watchdog(
            udid,
            runner,
            1,
            connection_lease=old,
            connection_engine=old_engine,
        )
    )
    tunnel_manager._tunnel_watchdogs[udid] = old_watchdog

    wifi_started = asyncio.Event()
    usb_started = asyncio.Event()
    lifecycle = tunnel_manager._get_tunnel_lifecycle_lock()

    async def concurrent_wifi_r2() -> None:
        wifi_started.set()
        async with lifecycle:
            await dm.connect_wifi_tunnel_owned("192.0.2.13", 49155)
            trace.append("WiFi R2/C2.commit")

    async def concurrent_usb_c2() -> None:
        usb_started.set()
        await dm.install_usb_c2()

    wifi_r2: asyncio.Task | None = None
    usb_c2: asyncio.Task | None = None
    try:
        await asyncio.wait_for(loss_started.wait(), timeout=0.5)
        dm._lock.arm_attempt_observer()
        wifi_r2 = asyncio.create_task(concurrent_wifi_r2())
        usb_c2 = asyncio.create_task(concurrent_usb_c2())
        await asyncio.wait_for(wifi_started.wait(), timeout=0.5)
        await asyncio.wait_for(usb_started.wait(), timeout=0.5)
        await asyncio.wait_for(dm._lock.acquire_attempted.wait(), timeout=0.5)
        await asyncio.sleep(0)
        assert not wifi_r2.done()
        assert not usb_c2.done()
        assert "WiFi R2/C2.commit" not in trace
        assert "USB C2.commit" not in trace

        release_loss.set()
        await asyncio.wait_for(old_watchdog, timeout=0.5)
        await asyncio.wait_for(wifi_r2, timeout=0.5)
        await asyncio.wait_for(usb_c2, timeout=0.5)
    finally:
        release_loss.set()
        for task in (old_watchdog, wifi_r2, usb_c2):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            old_watchdog,
            *(task for task in (wifi_r2, usb_c2) if task is not None),
            return_exceptions=True,
        )
        tunnel_manager._tunnels.pop(udid, None)
        tunnel_manager._tunnel_watchdogs.pop(udid, None)
        tunnel_manager._tunnel_generations.pop(udid, None)
        if dm._lock.locked():
            dm._lock.release()
        if lifecycle.locked():
            lifecycle.release()

    assert trace.index("tunnel_lost.exit") < trace.index("WiFi R2/C2.commit")
    assert trace.index("tunnel_lost.exit") < trace.index("USB C2.commit")

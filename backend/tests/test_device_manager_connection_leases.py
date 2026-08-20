"""Focused regressions for DeviceManager WiFi connection lease rollback."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import api.websocket as websocket
import core.device_manager as device_manager_module
from core.device_manager import DeviceManager, _ActiveConnection


class _FakeRsd:
    """Minimal successful RSD handshake used by the owned-connect tests."""

    def __init__(self, address: tuple[str, int]) -> None:
        self.address = address
        self.peer_info = {
            "Properties": {
                "UniqueDeviceID": "udid-lease",
                "OSVersion": "17.5",
                "DeviceClass": "iPhone",
            }
        }
        self.all_values = {"DeviceName": "Lease phone"}

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None


def _connection(name: str) -> _ActiveConnection:
    return _ActiveConnection(
        udid="udid-lease",
        lockdown=object(),
        ios_version="17.5",
        connection_type="Network",
        name=name,
    )


def _patch_successful_rsd(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(device_manager_module, "RemoteServiceDiscoveryService", _FakeRsd)
    # The test must not write the developer's persistent device-name cache.
    monkeypatch.setattr(device_manager_module, "_remember_device_name", lambda *_args: None)


class _CallbackRsd:
    """RSD double that proves uninstalled C1 is drained on callback failure."""

    instances: list["_CallbackRsd"] = []

    def __init__(self, address: tuple[str, int]) -> None:
        self.address = address
        self.peer_info = {
            "Properties": {
                "UniqueDeviceID": "udid-lease",
                "OSVersion": "17.5",
                "DeviceClass": "iPhone",
            }
        }
        self.all_values = {"DeviceName": "Lease phone"}
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
    monkeypatch.setattr(device_manager_module, "_remember_device_name", lambda *_args: None)


async def test_owned_connect_cancellation_drains_previous_and_rolls_back_new_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancel after C1 install: C0 and the exact C1 lease are both closed."""

    _patch_successful_rsd(monkeypatch)
    manager = DeviceManager()
    old = _connection("C0")
    manager._connections[old.udid] = old

    old_close_started = asyncio.Event()
    release_old_close = asyncio.Event()
    new_close_started = asyncio.Event()
    release_new_close = asyncio.Event()
    closed: list[_ActiveConnection] = []
    new_lease: _ActiveConnection | None = None

    async def close_detached(_udid: str, conn: _ActiveConnection) -> None:
        closed.append(conn)
        if conn is old:
            old_close_started.set()
            await release_old_close.wait()
        elif conn is new_lease:
            new_close_started.set()
            await release_new_close.wait()

    monkeypatch.setattr(manager, "_close_detached_connection", close_detached)

    start = asyncio.create_task(manager.connect_wifi_tunnel_owned("192.0.2.10", 49152))
    await asyncio.wait_for(old_close_started.wait(), timeout=0.5)
    new_lease = manager._connections[old.udid]
    assert new_lease is not old

    start.cancel()
    release_old_close.set()
    await asyncio.wait_for(new_close_started.wait(), timeout=0.5)
    release_new_close.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(start, timeout=0.5)

    assert closed == [old, new_lease]
    assert old.udid not in manager._connections


async def test_owned_connect_rollback_preserves_simultaneous_replacement_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A C2 replacement during C1 rollback remains fully owned by C2."""

    _patch_successful_rsd(monkeypatch)
    manager = DeviceManager()
    old = _connection("C0")
    replacement = _connection("C2")
    manager._connections[old.udid] = old

    old_close_started = asyncio.Event()
    release_old_close = asyncio.Event()
    new_close_started = asyncio.Event()
    release_new_close = asyncio.Event()
    closed: list[_ActiveConnection] = []
    new_lease: _ActiveConnection | None = None

    async def close_detached(_udid: str, conn: _ActiveConnection) -> None:
        closed.append(conn)
        if conn is old:
            old_close_started.set()
            await release_old_close.wait()
        elif conn is new_lease:
            # The exact C1 lease has already been detached by the rollback
            # CAS.  Installing C2 here models a concurrent reconnect at the
            # cleanup boundary, where stale cleanup must not touch C2.
            async with manager._lock:
                assert manager._connections.get(_udid) is None
                manager._connections[_udid] = replacement
            new_close_started.set()
            await release_new_close.wait()

    monkeypatch.setattr(manager, "_close_detached_connection", close_detached)

    start = asyncio.create_task(manager.connect_wifi_tunnel_owned("192.0.2.10", 49152))
    await asyncio.wait_for(old_close_started.wait(), timeout=0.5)
    new_lease = manager._connections[old.udid]
    assert new_lease is not old

    start.cancel()
    release_old_close.set()
    await asyncio.wait_for(new_close_started.wait(), timeout=0.5)
    release_new_close.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(start, timeout=0.5)

    assert closed == [old, new_lease]
    assert manager._connections[old.udid] is replacement
    assert replacement not in closed


async def test_owned_connect_cancellation_during_rsd_connect_closes_partial_rsd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation during the handshake must retrieve the partial RSD."""

    connect_started = asyncio.Event()
    release_connect = asyncio.Event()
    instances: list[object] = []

    class _BlockingRsd:
        def __init__(self, address: tuple[str, int]) -> None:
            self.address = address
            self.peer_info = {}
            self.all_values = {}
            self.close_calls = 0
            instances.append(self)

        async def connect(self) -> None:
            connect_started.set()
            await release_connect.wait()

        async def close(self) -> None:
            self.close_calls += 1

    monkeypatch.setattr(device_manager_module, "RemoteServiceDiscoveryService", _BlockingRsd)
    manager = DeviceManager()
    connect = asyncio.create_task(
        manager.connect_wifi_tunnel_owned("192.0.2.10", 49152),
    )
    try:
        await asyncio.wait_for(connect_started.wait(), timeout=0.5)
        connect.cancel()
        release_connect.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(connect, timeout=0.5)
    finally:
        release_connect.set()
        if not connect.done():
            connect.cancel()
        await asyncio.gather(connect, return_exceptions=True)

    assert len(instances) == 1
    assert instances[0].close_calls == 1
    assert manager._connections == {}


async def test_owned_connect_callback_is_before_install_and_previous_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blocked replacement callback leaves C0 current and open."""

    _patch_callback_rsd(monkeypatch)
    manager = DeviceManager()
    old = _connection("C0")
    manager._connections[old.udid] = old
    callback_started = asyncio.Event()
    release_callback = asyncio.Event()
    callback_current: list[object] = []
    closed: list[_ActiveConnection] = []
    original_close = manager._close_detached_connection

    async def close(udid: str, conn: _ActiveConnection) -> None:
        closed.append(conn)
        await original_close(udid, conn)

    async def before_replace(*_args) -> None:
        callback_current.append(manager._connections.get(old.udid))
        callback_started.set()
        await release_callback.wait()

    monkeypatch.setattr(manager, "_close_detached_connection", close)
    start = asyncio.create_task(
        manager.connect_wifi_tunnel_owned(
            "192.0.2.10",
            49152,
            before_close_previous=before_replace,
        ),
    )
    await asyncio.wait_for(callback_started.wait(), timeout=0.5)

    assert manager._connections[old.udid] is old
    assert callback_current == [old]
    assert closed == []
    assert _CallbackRsd.instances[-1].close_calls == 0

    release_callback.set()
    info, new = await asyncio.wait_for(start, timeout=0.5)
    assert info.udid == old.udid
    assert manager._connections[old.udid] is new
    assert new is not old
    assert closed == [old]
    assert _CallbackRsd.instances[-1].close_calls == 0


async def test_owned_connect_callback_success_closes_previous_after_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful callback permits C1 install followed by C0 close."""

    _patch_callback_rsd(monkeypatch)
    manager = DeviceManager()
    old = _connection("C0")
    manager._connections[old.udid] = old
    callback_current: list[object] = []
    closed: list[_ActiveConnection] = []
    original_close = manager._close_detached_connection

    async def close(udid: str, conn: _ActiveConnection) -> None:
        closed.append(conn)
        await original_close(udid, conn)

    async def before_replace(*_args) -> None:
        callback_current.append(manager._connections.get(old.udid))

    monkeypatch.setattr(manager, "_close_detached_connection", close)
    info, new = await manager.connect_wifi_tunnel_owned(
        "192.0.2.10",
        49152,
        before_close_previous=before_replace,
    )

    assert info.udid == old.udid
    assert callback_current == [old]
    assert manager._connections[old.udid] is new
    assert closed == [old]
    assert _CallbackRsd.instances[-1].close_calls == 0


async def test_owned_connect_callback_error_closes_uninstalled_c1_and_keeps_c0(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Callback failure drains the exact C0 and uninstalled C1."""

    _patch_callback_rsd(monkeypatch)
    manager = DeviceManager()
    old = _connection("C0")
    manager._connections[old.udid] = old
    closed: list[_ActiveConnection] = []
    original_close = manager._close_detached_connection

    async def close(udid: str, conn: _ActiveConnection) -> None:
        closed.append(conn)
        await original_close(udid, conn)

    async def before_replace(*_args) -> None:
        raise RuntimeError("pre-replace drain failed")

    monkeypatch.setattr(manager, "_close_detached_connection", close)
    with pytest.raises(RuntimeError, match="pre-replace drain failed"):
        await manager.connect_wifi_tunnel_owned(
            "192.0.2.10",
            49152,
            before_close_previous=before_replace,
        )

    assert old.udid not in manager._connections
    assert closed == [old]
    assert _CallbackRsd.instances[-1].close_calls == 1


async def test_owned_connect_callback_cancellation_closes_c1_and_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Callback cancellation drains exact C0/C1 and propagates cancellation."""

    _patch_callback_rsd(monkeypatch)
    manager = DeviceManager()
    old = _connection("C0")
    manager._connections[old.udid] = old
    callback_started = asyncio.Event()
    release_callback = asyncio.Event()
    closed: list[_ActiveConnection] = []
    original_close = manager._close_detached_connection

    async def close(udid: str, conn: _ActiveConnection) -> None:
        closed.append(conn)
        await original_close(udid, conn)

    async def before_replace(*_args) -> None:
        callback_started.set()
        await release_callback.wait()

    monkeypatch.setattr(manager, "_close_detached_connection", close)
    start = asyncio.create_task(
        manager.connect_wifi_tunnel_owned(
            "192.0.2.10",
            49152,
            before_close_previous=before_replace,
        ),
    )
    try:
        await asyncio.wait_for(callback_started.wait(), timeout=0.5)
        start.cancel()
        # DeviceManager drains the callback transaction before re-propagating
        # caller cancellation; release the callback's deterministic child so
        # that drain can finish rather than leaving a shielded task pending.
        release_callback.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(start, timeout=0.5)
    finally:
        release_callback.set()
        if not start.done():
            start.cancel()
        await asyncio.gather(start, return_exceptions=True)

    assert old.udid not in manager._connections
    assert closed == [old]
    assert _CallbackRsd.instances[-1].close_calls == 1


async def test_owned_connect_callback_runs_without_previous_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The callback contract is invoked even when no C0 predecessor exists."""

    _patch_callback_rsd(monkeypatch)
    manager = DeviceManager()
    callback_seen: list[object] = []

    async def before_replace(*_args) -> None:
        callback_seen.append(manager._connections.get("udid-lease"))

    info, lease = await manager.connect_wifi_tunnel_owned(
        "192.0.2.10",
        49152,
        before_close_previous=before_replace,
    )

    assert info.udid == "udid-lease"
    assert callback_seen == [None]
    assert manager._connections[info.udid] is lease
    assert _CallbackRsd.instances[-1].close_calls == 0


async def test_owned_connect_callback_cas_conflict_closes_c1_and_preserves_newer_c2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lease installed during the callback wins over stale C1 adoption."""

    _patch_callback_rsd(monkeypatch)
    manager = DeviceManager()
    old = _connection("C0")
    replacement = _connection("C2")
    manager._connections[old.udid] = old
    callback_started = asyncio.Event()
    release_callback = asyncio.Event()

    async def before_replace(*_args) -> None:
        callback_started.set()
        await release_callback.wait()

    start = asyncio.create_task(
        manager.connect_wifi_tunnel_owned(
            "192.0.2.10",
            49152,
            before_close_previous=before_replace,
        ),
    )
    await asyncio.wait_for(callback_started.wait(), timeout=0.5)
    manager._connections[old.udid] = replacement
    release_callback.set()

    with pytest.raises(RuntimeError, match="adoption conflict"):
        await asyncio.wait_for(start, timeout=0.5)

    assert manager._connections[old.udid] is replacement
    assert _CallbackRsd.instances[-1].close_calls == 1


async def test_owned_connect_destructive_callback_cancel_drains_c0_and_c1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation after destructive E0 quiesce cannot strand C0 in DM."""

    _patch_callback_rsd(monkeypatch)
    manager = DeviceManager()
    old = _connection("C0")
    manager._connections[old.udid] = old
    old_engine = {"dead": False}
    engine_registry: dict[str, object] = {old.udid: old_engine}
    callback_started = asyncio.Event()
    release_callback = asyncio.Event()
    closed: list[_ActiveConnection] = []
    original_close = manager._close_detached_connection

    async def close(udid: str, conn: _ActiveConnection) -> None:
        closed.append(conn)
        setattr(conn, "_test_closed", True)
        await original_close(udid, conn)

    async def before_close(udid: str, previous: _ActiveConnection | None) -> None:
        assert udid == old.udid
        assert previous is old
        assert manager._connections[udid] is old
        # Model the route's destructive quiesce: E0 is marked dead and
        # removed before the callback reaches its cancellation barrier.
        old_engine["dead"] = True
        engine_registry.pop(udid)
        callback_started.set()
        await release_callback.wait()

    monkeypatch.setattr(manager, "_close_detached_connection", close)
    start = asyncio.create_task(
        manager.connect_wifi_tunnel_owned(
            "192.0.2.10",
            49152,
            before_close_previous=before_close,
        ),
    )
    try:
        await asyncio.wait_for(callback_started.wait(), timeout=0.5)
        assert manager._connections[old.udid] is old
        assert old.udid not in engine_registry
        start.cancel()
        release_callback.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(start, timeout=0.5)
    finally:
        release_callback.set()
        if not start.done():
            start.cancel()
        await asyncio.gather(start, return_exceptions=True)

    # The callback made E0 unusable while C0 was still current.  Aborting the
    # replacement must therefore close the exact C0 as well as uninstalled C1
    # and leave no stale C0 registry entry behind.
    assert old.udid not in manager._connections
    assert old.udid not in engine_registry
    assert old_engine["dead"]
    assert closed.count(old) == 1
    assert getattr(old, "_test_closed", False)
    assert _CallbackRsd.instances[-1].close_calls == 1


async def test_owned_connect_destructive_callback_error_drains_c0_and_c1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ordinary error after E0 quiesce leaves one coherent detached state."""

    _patch_callback_rsd(monkeypatch)
    manager = DeviceManager()
    old = _connection("C0")
    manager._connections[old.udid] = old
    old_engine = {"dead": False}
    engine_registry: dict[str, object] = {old.udid: old_engine}
    closed: list[_ActiveConnection] = []
    original_close = manager._close_detached_connection

    async def close(udid: str, conn: _ActiveConnection) -> None:
        closed.append(conn)
        setattr(conn, "_test_closed", True)
        await original_close(udid, conn)

    async def before_close(udid: str, previous: _ActiveConnection | None) -> None:
        assert udid == old.udid
        assert previous is old
        assert manager._connections[udid] is old
        old_engine["dead"] = True
        engine_registry.pop(udid)
        raise RuntimeError("destructive quiesce failed")

    monkeypatch.setattr(manager, "_close_detached_connection", close)
    with pytest.raises(RuntimeError, match="destructive quiesce failed"):
        await manager.connect_wifi_tunnel_owned(
            "192.0.2.10",
            49152,
            before_close_previous=before_close,
        )

    assert old.udid not in manager._connections
    assert old.udid not in engine_registry
    assert old_engine["dead"]
    assert closed.count(old) == 1
    assert getattr(old, "_test_closed", False)
    assert _CallbackRsd.instances[-1].close_calls == 1


async def test_owned_connect_destructive_abort_preserves_concurrent_c2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Abort cleanup closes C0/C1 but never steals a concurrent C2 lease."""

    _patch_callback_rsd(monkeypatch)
    manager = DeviceManager()
    old = _connection("C0")
    manager._connections[old.udid] = old
    old_engine = {"dead": False}
    engine_registry: dict[str, object] = {old.udid: old_engine}
    callback_started = asyncio.Event()
    release_callback = asyncio.Event()
    closed: list[_ActiveConnection] = []
    original_close = manager._close_detached_connection

    async def close(udid: str, conn: _ActiveConnection) -> None:
        closed.append(conn)
        setattr(conn, "_test_closed", True)
        await original_close(udid, conn)

    async def before_close(udid: str, previous: _ActiveConnection | None) -> None:
        assert udid == old.udid
        assert previous is old
        old_engine["dead"] = True
        engine_registry.pop(udid)
        callback_started.set()
        await release_callback.wait()

    monkeypatch.setattr(manager, "_close_detached_connection", close)
    start = asyncio.create_task(
        manager.connect_wifi_tunnel_owned(
            "192.0.2.10",
            49152,
            before_close_previous=before_close,
        ),
    )
    try:
        await asyncio.wait_for(callback_started.wait(), timeout=0.5)
        first_c1_rsd = _CallbackRsd.instances[-1]
        # Use a real second DeviceManager connect to model C2 winning the
        # concurrent replacement race.  That owner closes its exact C0; the
        # aborting C1 task must not subsequently detach or close C2.
        _, replacement = await manager.connect_wifi_tunnel_owned(
            "192.0.2.11",
            49153,
        )
        second_c2_rsd = _CallbackRsd.instances[-1]
        assert manager._connections[old.udid] is replacement
        start.cancel()
        release_callback.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(start, timeout=0.5)
    finally:
        release_callback.set()
        if not start.done():
            start.cancel()
        await asyncio.gather(start, return_exceptions=True)

    assert manager._connections[old.udid] is replacement
    assert replacement not in closed
    assert not getattr(replacement, "_test_closed", False)
    assert old.udid not in engine_registry
    assert old_engine["dead"]
    assert closed.count(old) == 1
    assert getattr(old, "_test_closed", False)
    assert first_c1_rsd.close_calls == 1
    assert second_c2_rsd.close_calls == 0


async def test_owned_connect_destructive_callback_cancel_during_cas_install_drains_c0(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation blocked on C1 CAS-install cannot strand dead C0/E0."""

    _patch_callback_rsd(monkeypatch)
    manager = DeviceManager()
    old = _connection("C0")
    manager._connections[old.udid] = old
    old_engine = {"dead": False}
    engine_registry: dict[str, object] = {old.udid: old_engine}
    callback_started = asyncio.Event()
    callback_finished = asyncio.Event()
    release_callback = asyncio.Event()
    closed: list[_ActiveConnection] = []
    original_close = manager._close_detached_connection

    async def close(udid: str, conn: _ActiveConnection) -> None:
        closed.append(conn)
        setattr(conn, "_test_closed", True)
        await original_close(udid, conn)

    async def before_close(udid: str, previous: _ActiveConnection | None) -> None:
        assert udid == old.udid
        assert previous is old
        old_engine["dead"] = True
        engine_registry.pop(udid)
        callback_started.set()
        await release_callback.wait()
        callback_finished.set()

    monkeypatch.setattr(manager, "_close_detached_connection", close)
    start = asyncio.create_task(
        manager.connect_wifi_tunnel_owned(
            "192.0.2.10",
            49152,
            before_close_previous=before_close,
        ),
    )
    lock_held = False
    try:
        await asyncio.wait_for(callback_started.wait(), timeout=0.5)
        # Hold the manager lock across callback success so the C1 CAS-install
        # is definitely waiting when the caller cancellation arrives.
        await manager._lock.acquire()
        lock_held = True
        release_callback.set()
        await asyncio.wait_for(callback_finished.wait(), timeout=0.5)
        await asyncio.sleep(0)
        assert not start.done()
        start.cancel()
        manager._lock.release()
        lock_held = False
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(start, timeout=0.5)
    finally:
        release_callback.set()
        if lock_held:
            manager._lock.release()
        if not start.done():
            start.cancel()
        await asyncio.gather(start, return_exceptions=True)

    assert old.udid not in manager._connections
    assert old.udid not in engine_registry
    assert old_engine["dead"]
    assert closed.count(old) == 1
    assert getattr(old, "_test_closed", False)
    assert _CallbackRsd.instances[-1].close_calls == 1


async def test_owned_connect_destructive_abort_broadcasts_device_disconnected_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancelled destructive adoption reports the exact detached C0 once."""

    _patch_callback_rsd(monkeypatch)
    manager = DeviceManager()
    old = _connection("C0")
    manager._connections[old.udid] = old
    callback_started = asyncio.Event()
    release_callback = asyncio.Event()
    events: list[tuple[str, dict]] = []

    import main

    old_engine = SimpleNamespace()
    monkeypatch.setattr(main.app_state, "simulation_engines", {old.udid: old_engine})
    monkeypatch.setattr(main.app_state, "_primary_udid", old.udid)

    async def broadcast(event_type: str, payload: dict) -> None:
        events.append((event_type, dict(payload)))

    monkeypatch.setattr(websocket, "broadcast", broadcast)

    async def before_close(udid: str, previous: _ActiveConnection | None) -> None:
        assert udid == old.udid
        assert previous is old
        # Model the route's destructive engine quiesce.  C0 remains the
        # current DM lease until DeviceManager's abort path exact-detaches it.
        main.app_state.simulation_engines.pop(udid, None)
        callback_started.set()
        await release_callback.wait()

    start = asyncio.create_task(
        manager.connect_wifi_tunnel_owned(
            "192.0.2.10",
            49152,
            before_close_previous=before_close,
        ),
    )
    try:
        await asyncio.wait_for(callback_started.wait(), timeout=0.5)
        start.cancel()
        release_callback.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(start, timeout=0.5)
    finally:
        release_callback.set()
        if not start.done():
            start.cancel()
        await asyncio.gather(start, return_exceptions=True)

    disconnected = [
        (event_type, payload)
        for event_type, payload in events
        if event_type == "device_disconnected"
    ]
    assert len(disconnected) == 1
    assert disconnected[0][1]["udid"] == old.udid
    assert disconnected[0][1]["udids"] == [old.udid]
    assert manager._connections == {}
    assert _CallbackRsd.instances[-1].close_calls == 1


async def test_owned_connect_destructive_abort_does_not_broadcast_stale_c2_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exact-C0 abort stays silent after a concurrent C2 wins the CAS."""

    _patch_callback_rsd(monkeypatch)
    manager = DeviceManager()
    old = _connection("C0")
    replacement = _connection("C2")
    manager._connections[old.udid] = old
    callback_started = asyncio.Event()
    release_callback = asyncio.Event()
    events: list[tuple[str, dict]] = []

    async def broadcast(event_type: str, payload: dict) -> None:
        events.append((event_type, dict(payload)))

    monkeypatch.setattr(websocket, "broadcast", broadcast)

    async def before_close(udid: str, previous: _ActiveConnection | None) -> None:
        assert udid == old.udid
        assert previous is old
        callback_started.set()
        await release_callback.wait()

    start = asyncio.create_task(
        manager.connect_wifi_tunnel_owned(
            "192.0.2.10",
            49152,
            before_close_previous=before_close,
        ),
    )
    try:
        await asyncio.wait_for(callback_started.wait(), timeout=0.5)
        # C2 wins before the cancelled C1 callback reaches its exact-detach
        # abort.  The stale C1 owner must not announce C0 as disconnected.
        manager._connections[old.udid] = replacement
        start.cancel()
        release_callback.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(start, timeout=0.5)
    finally:
        release_callback.set()
        if not start.done():
            start.cancel()
        await asyncio.gather(start, return_exceptions=True)

    assert manager._connections[old.udid] is replacement
    assert [event for event, _payload in events if event == "device_disconnected"] == []
    assert _CallbackRsd.instances[-1].close_calls == 1

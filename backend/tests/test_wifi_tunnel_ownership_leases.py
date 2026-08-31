"""Deterministic regressions for WiFi connection/runner ownership leases."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from api import device
import services.tunnel_manager as tunnel_manager


class _Connection:
    def __init__(self, name: str) -> None:
        self.name = name
        self.connection_type = "Network"


class _Engine:
    def __init__(
        self,
        name: str,
        *,
        emit_started: asyncio.Event | None = None,
        release_emit: asyncio.Event | None = None,
    ) -> None:
        self.name = name
        self.state = None
        self._stop_event = asyncio.Event()
        self._pause_event = asyncio.Event()
        self._active_task: asyncio.Task | None = None
        self.active_done = asyncio.Event()
        self.emit_started = emit_started
        self.release_emit = release_emit
        self.emit_calls = 0

    async def _emit(self, *_args) -> None:
        self.emit_calls += 1
        if self.emit_started is not None:
            self.emit_started.set()
        if self.release_emit is not None:
            await self.release_emit.wait()

    async def run_active(self) -> None:
        try:
            await self._stop_event.wait()
        finally:
            self.active_done.set()


class _BlockingQuiesceEngine(_Engine):
    """Engine fake whose cancellation drain is observable and deterministic."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.active_started = asyncio.Event()
        self.cancel_seen = asyncio.Event()
        self.release_drain = asyncio.Event()

    async def run_active(self) -> None:
        self.active_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancel_seen.set()
            await self.release_drain.wait()
            raise
        finally:
            self.active_done.set()


class _BlockingQuiesceEngineWithStop(_BlockingQuiesceEngine):
    """Production-like engine exposing an async stop hook."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.stop_started = asyncio.Event()
        self.stop_completed = asyncio.Event()

    async def stop(self) -> None:
        self.stop_started.set()
        self._stop_event.set()
        self._pause_event.set()
        active = self._active_task
        if active is not None and not active.done():
            active.cancel()
        if active is not None:
            await asyncio.gather(active, return_exceptions=True)
        self.stop_completed.set()


class _TraceEngine(_Engine):
    """Small engine double that records the lifecycle ordering contract."""

    def __init__(self, name: str, trace: list[str]) -> None:
        super().__init__(name)
        self.trace = trace

    async def stop(self) -> None:
        self.trace.append(f"{self.name}.stop_started")
        self._stop_event.set()
        self._pause_event.set()
        active = self._active_task
        if active is not None and not active.done():
            active.cancel()
        if active is not None:
            await asyncio.gather(active, return_exceptions=True)
        self.trace.append(f"{self.name}.stop_completed")

    async def resume_from_snapshot(self, _snapshot: dict) -> None:
        self.trace.append(f"{self.name}.resume")


class _Runner:
    instances: list["_Runner"] = []

    def __init__(self, name: str) -> None:
        self.name = name
        self.info: dict | None = None
        self.target_ip: str | None = None
        self.target_port: int | None = None
        self.task: asyncio.Task | None = None
        self._hold = asyncio.Event()
        self.stop_calls = 0
        self.instances.append(self)

    def is_running(self) -> bool:
        return self.task is not None and not self.task.done()

    async def _run(self) -> None:
        await self._hold.wait()

    async def start(self, _udid: str, ip: str, port: int, timeout: float = 10.0) -> dict:
        self.target_ip = ip
        self.target_port = port
        self.info = {
            "rsd_address": f"fd00::{self.name}",
            "rsd_port": 12345,
            "interface": "fake",
        }
        self.task = asyncio.create_task(self._run())
        return dict(self.info)

    async def stop(self) -> None:
        self.stop_calls += 1
        self._hold.set()
        if self.task is not None and not self.task.done():
            self.task.cancel()
        if self.task is not None:
            await asyncio.gather(self.task, return_exceptions=True)


class _LeaseDeviceManager:
    def __init__(self, udid: str) -> None:
        self.udid = udid
        self._connections: dict[str, _Connection] = {}
        self.connect_count = 0
        self.disconnect_calls: list[tuple[str, _Connection | None]] = []
        self.close_calls: list[tuple[str, _Connection]] = []

    async def connect_wifi_tunnel_owned(
        self,
        _address: str,
        _port: int,
        before_close_previous=None,
        **kwargs,
    ) -> tuple[SimpleNamespace, _Connection]:
        previous = self._connections.get(self.udid)
        callback = before_close_previous
        if callback is None:
            callback = next(
                (value for value in kwargs.values() if callable(value)),
                None,
            )
        if callback is not None:
            result = callback(self.udid, previous)
            if asyncio.iscoroutine(result):
                await result
        self.connect_count += 1
        connection = _Connection(f"C{self.connect_count}")
        self._connections[self.udid] = connection
        info = SimpleNamespace(
            udid=self.udid,
            name="Phone",
            ios_version="17.5",
        )
        return info, connection

    async def connect_wifi_tunnel(self, address: str, port: int) -> SimpleNamespace:
        info, _lease = await self.connect_wifi_tunnel_owned(address, port)
        return info

    async def _detach_connection(
        self,
        udid: str,
        *,
        expected: _Connection | None = None,
    ) -> _Connection | None:
        current = self._connections.get(udid)
        if expected is not None and current is not expected:
            return None
        return self._connections.pop(udid, None)

    async def _close_detached_connection(self, udid: str, conn: _Connection) -> None:
        self.close_calls.append((udid, conn))

    async def disconnect_if_current(self, udid: str, expected: _Connection) -> bool:
        conn = await self._detach_connection(udid, expected=expected)
        if conn is None:
            return False
        self.disconnect_calls.append((udid, conn))
        await self._close_detached_connection(udid, conn)
        return True

    async def disconnect(
        self,
        udid: str,
        *,
        expected: _Connection | None = None,
    ) -> None:
        conn = await self._detach_connection(udid, expected=expected)
        if conn is None:
            return
        self.disconnect_calls.append((udid, conn))
        await self._close_detached_connection(udid, conn)

    async def discover_devices(self) -> list[object]:
        return []


class _CountingConnections(dict[str, _Connection]):
    def __init__(self, owner: "_CapRaceDeviceManager") -> None:
        super().__init__()
        self.owner = owner

    def __len__(self) -> int:
        self.owner.len_calls += 1
        if self.owner.len_calls >= 3:
            self.owner.outer_prechecks_done.set()
        return super().__len__()


class _CapRaceDeviceManager(_LeaseDeviceManager):
    """DM fake that makes the A/B cap race observable at both boundaries."""

    def __init__(self) -> None:
        super().__init__("udid-a")
        self.outer_prechecks_done = asyncio.Event()
        self.a_connect_started = asyncio.Event()
        self.release_a_connect = asyncio.Event()
        self.len_calls = 0
        self.connect_addresses: list[str] = []
        self._connections = _CountingConnections(self)
        self._connections.update(
            {
                "existing-a": _Connection("C0a"),
                "existing-b": _Connection("C0b"),
            }
        )

    async def connect_wifi_tunnel_owned(
        self,
        address: str,
        _port: int,
        before_close_previous=None,
        **kwargs,
    ) -> tuple[SimpleNamespace, _Connection]:
        target_udid = "udid-a" if address == "fd00::A" else "udid-b"
        previous = self._connections.get(target_udid)
        callback = before_close_previous
        if callback is None:
            callback = next(
                (value for value in kwargs.values() if callable(value)),
                None,
            )
        if callback is not None:
            result = callback(target_udid, previous)
            if asyncio.iscoroutine(result):
                await result
        self.connect_count += 1
        self.connect_addresses.append(address)
        if address == "fd00::A":
            self.a_connect_started.set()
            await self.release_a_connect.wait()
            udid = "udid-a"
        else:
            udid = "udid-b"
        connection = _Connection(f"C{self.connect_count + 2}")
        self._connections[udid] = connection
        return (
            SimpleNamespace(udid=udid, name="Phone", ios_version="17.5"),
            connection,
        )


class _OrderedReplacementDeviceManager(_LeaseDeviceManager):
    """Faithful replacement fake for the pre-close callback contract.

    The production manager owns the C1/C0 transaction.  This fake deliberately
    keeps C0 current while ``before_close_previous`` runs, then installs C1 and closes
    the detached C0.  Until the routes pass the callback, the resulting trace
    exposes the old close-before-engine-stop ordering.
    """

    def __init__(self, udid: str, trace: list[str]) -> None:
        super().__init__(udid)
        self.trace = trace

    async def connect_wifi_tunnel_owned(
        self,
        _address: str,
        _port: int,
        before_close_previous=None,
        **kwargs,
    ) -> tuple[SimpleNamespace, _Connection]:
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

        self.connect_count += 1
        connection = _Connection(f"C{self.connect_count}")
        self._connections[self.udid] = connection
        if previous is not None and self._connections.get(self.udid) is connection:
            await self._close_detached_connection(self.udid, previous)
        return (
            SimpleNamespace(udid=self.udid, name="Phone", ios_version="17.5"),
            connection,
        )

    async def _close_detached_connection(
        self,
        udid: str,
        conn: _Connection,
    ) -> None:
        self.trace.append(f"{conn.name}.close")
        await super()._close_detached_connection(udid, conn)


@pytest.fixture(autouse=True)
async def clean_tunnel_manager_state() -> None:
    async def drain() -> None:
        watchdogs = list(tunnel_manager._tunnel_watchdogs.values())
        for task in watchdogs:
            if not task.done():
                task.cancel()
        if watchdogs:
            await asyncio.gather(*watchdogs, return_exceptions=True)

        side_effects = [
            task
            for tasks in tunnel_manager._tunnel_side_effects.values()
            for task in tasks
        ]
        for task in side_effects:
            if not task.done():
                task.cancel()
        if side_effects:
            await asyncio.gather(*side_effects, return_exceptions=True)

        runners = list(tunnel_manager._tunnels.values())
        for runner in runners:
            stop = getattr(runner, "stop", None)
            if stop is not None:
                await stop()
        tunnel_manager._tunnel_watchdogs.clear()
        tunnel_manager._tunnels.clear()
        tunnel_manager._tunnel_side_effects.clear()
        tunnel_manager._tunnel_generations.clear()
        tunnel_manager._pending_tunnel_starts.clear()
        tunnel_manager._tunnel_stop_watermarks.clear()
        tunnel_manager._tunnel_start_sequence = 0
        tunnel_manager._tunnel_stop_all_watermark = 0
        _Runner.instances.clear()

    await drain()
    yield
    await drain()


def _patch_state(monkeypatch: pytest.MonkeyPatch, create_engine):
    import main

    state = main.app_state
    monkeypatch.setattr(state, "simulation_engines", {})
    monkeypatch.setattr(state, "_primary_udid", None)
    monkeypatch.setattr(state, "create_engine_for_device", create_engine)
    return state


def _patch_device_manager(monkeypatch: pytest.MonkeyPatch, dm: _LeaseDeviceManager) -> None:
    monkeypatch.setattr(device, "_dm", lambda: dm)
    monkeypatch.setattr(tunnel_manager, "_dm", lambda: dm)


def _patch_broadcast(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []

    async def broadcast(event_type: str, data: dict) -> None:
        events.append((event_type, data))

    monkeypatch.setattr("api.websocket.broadcast", broadcast)
    return events


async def test_legacy_direct_connect_rechecks_cap_inside_adoption_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A/B may pass the early cap check, but only A may install the last slot."""

    dm = _CapRaceDeviceManager()
    _patch_device_manager(monkeypatch, dm)
    events = _patch_broadcast(monkeypatch)
    engines: dict[str, _Engine] = {}

    async def create_engine(udid: str) -> None:
        engine = _Engine(f"E-{udid}")
        engines[udid] = engine
        state.simulation_engines[udid] = engine

    state = _patch_state(monkeypatch, create_engine)
    first = asyncio.create_task(
        device.wifi_tunnel_connect(
            device.WifiTunnelConnectRequest(
                rsd_address="fd00::A",
                rsd_port=12345,
            ),
        ),
    )
    second: asyncio.Task | None = None
    try:
        await asyncio.wait_for(dm.a_connect_started.wait(), timeout=0.5)
        second = asyncio.create_task(
            device.wifi_tunnel_connect(
                device.WifiTunnelConnectRequest(
                    rsd_address="fd00::B",
                    rsd_port=12345,
                ),
            ),
        )
        # A owns the adoption lock while its DM handshake is blocked.  B has
        # nevertheless completed the outer pre-check and is now waiting for
        # the serialized install boundary.
        await asyncio.wait_for(dm.outer_prechecks_done.wait(), timeout=0.5)
        assert not first.done()
        assert not second.done()
        assert dm.connect_addresses == ["fd00::A"]

        dm.release_a_connect.set()
        first_result = await asyncio.wait_for(first, timeout=0.5)
        assert first_result["status"] == "connected"
        assert dm._connections["udid-a"].name == "C3"

        with pytest.raises(HTTPException) as second_error:
            await asyncio.wait_for(second, timeout=0.5)
        assert second_error.value.status_code == 409
        assert second_error.value.detail["code"] == "max_devices_reached"
        assert dm.connect_addresses == ["fd00::A"]
        assert dm._connections["udid-a"].name == "C3"
        assert "udid-b" not in dm._connections
        assert set(state.simulation_engines) == {"udid-a"}
        assert state.simulation_engines["udid-a"] is engines["udid-a"]
        assert not any(event == "device_disconnected" for event, _ in events)
    finally:
        dm.release_a_connect.set()
        for task in (first, second):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (first, second) if task is not None),
            return_exceptions=True,
        )


async def test_old_stop_cleanup_cannot_remove_new_start_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Old cleanup quiesces E1 before replacement and cannot clobber C2/E2."""

    udid = "udid-1"
    temp_key = "pending:192.0.2.11:49152"
    dm = _LeaseDeviceManager(udid)
    _patch_device_manager(monkeypatch, dm)
    events = _patch_broadcast(monkeypatch)

    old_emit_started = asyncio.Event()
    release_old_emit = asyncio.Event()
    old_engine = _Engine(
        "E1",
        emit_started=old_emit_started,
        release_emit=release_old_emit,
    )
    old_engine._active_task = asyncio.create_task(old_engine.run_active())
    new_engine = _Engine("E2")
    state = _patch_state(monkeypatch, lambda _udid: _create_new_engine(state, new_engine))
    state.simulation_engines[udid] = old_engine
    state._primary_udid = udid
    dm.connect_count = 1
    dm._connections[udid] = _Connection("C1")

    old_runner = _Runner("R1")
    await old_runner.start(udid, "192.0.2.11", 49152)
    old_watchdog = asyncio.create_task(asyncio.Event().wait())
    tunnel_manager._tunnels[udid] = old_runner  # type: ignore[assignment]
    tunnel_manager._tunnel_watchdogs[udid] = old_watchdog
    tunnel_manager._tunnel_generations[udid] = 1

    runner_index = 0

    def make_runner() -> _Runner:
        nonlocal runner_index
        runner_index += 1
        return _Runner(f"R{runner_index + 1}")

    monkeypatch.setattr(device, "TunnelRunner", make_runner)
    monkeypatch.setattr(
        device,
        "_build_tunnel_udid_candidates",
        lambda _req: [temp_key],
    )

    async def idle_watchdog(*_args) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(device, "_per_tunnel_watchdog", idle_watchdog)

    stop_task = asyncio.create_task(
        device.wifi_tunnel_stop(device.WifiTunnelStopRequest(udid=udid)),
    )
    new_task: asyncio.Task | None = None
    try:
        await asyncio.wait_for(old_emit_started.wait(), timeout=0.5)
        assert udid not in tunnel_manager._tunnels

        new_task = asyncio.create_task(
            device.wifi_tunnel_start_and_connect(
                device.WifiTunnelStartRequest(
                    ip="192.0.2.11",
                    port=49152,
                ),
            ),
        )
        # The shared adoption lock deliberately keeps replacement pending
        # while the old exact engine lease is still blocked in its emit.
        await asyncio.sleep(0)
        assert not new_task.done()

        release_old_emit.set()
        stop_result = await asyncio.wait_for(stop_task, timeout=0.5)
        assert stop_result["status"] == "stopped"

        new_result = await asyncio.wait_for(new_task, timeout=0.5)
        assert new_result["status"] == "connected"
        assert dm._connections[udid].name == "C2"
        assert state.simulation_engines[udid] is new_engine
        assert tunnel_manager._tunnels[udid] is not old_runner

        assert tunnel_manager._tunnels[udid] is not old_runner
        assert dm._connections[udid].name == "C2"
        assert state.simulation_engines[udid] is new_engine
        # The old lifecycle may have emitted its own disconnect before the
        # replacement was admitted; it must not emit anything after C2/E2 is
        # installed or remove that newer lifecycle.
        assert [event for event, _ in events].count("device_disconnected") == 1
        await asyncio.wait_for(old_engine.active_done.wait(), timeout=0.5)
        assert old_engine._active_task is not None
        assert old_engine._active_task.done()
    finally:
        release_old_emit.set()
        if new_task is not None and not new_task.done():
            new_task.cancel()
        if not stop_task.done():
            stop_task.cancel()
        await asyncio.gather(new_task, stop_task, return_exceptions=True)
        if old_engine._active_task is not None and not old_engine._active_task.done():
            old_engine._active_task.cancel()
        if old_engine._active_task is not None:
            await asyncio.gather(old_engine._active_task, return_exceptions=True)
        runner, watchdog, side_effects = await tunnel_manager._detach_tunnel(udid)
        await tunnel_manager._stop_tunnel_parts(
            runner,
            watchdog,
            caller="test_new_generation_teardown",
            udid=udid,
            side_effects=side_effects,
        )
        await old_runner.stop()


async def test_stop_fence_detach_is_linearized_before_same_endpoint_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A newer same-endpoint start must not reuse a stop-doomed runner."""

    udid = "udid-1"
    temp_key = "pending:192.0.2.13:49152"
    dm = _LeaseDeviceManager(udid)
    _patch_device_manager(monkeypatch, dm)
    events = _patch_broadcast(monkeypatch)
    old_engine = _Engine("E1")
    new_engine = _Engine("E2")

    async def create_engine(_udid: str) -> None:
        state.simulation_engines[udid] = new_engine

    state = _patch_state(monkeypatch, create_engine)
    state.simulation_engines[udid] = old_engine
    state._primary_udid = udid
    dm.connect_count = 1
    dm._connections[udid] = _Connection("C1")

    old_runner = _Runner("R1")
    await old_runner.start(udid, "192.0.2.13", 49152)
    old_watchdog = asyncio.create_task(asyncio.Event().wait())
    tunnel_manager._tunnels[udid] = old_runner  # type: ignore[assignment]
    tunnel_manager._tunnel_watchdogs[udid] = old_watchdog
    tunnel_manager._tunnel_generations[udid] = 1

    new_runner = _Runner("R2")
    monkeypatch.setattr(device, "TunnelRunner", lambda: new_runner)
    monkeypatch.setattr(
        device,
        "_build_tunnel_udid_candidates",
        lambda _req: [temp_key],
    )

    async def idle_watchdog(*_args) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(device, "_per_tunnel_watchdog", idle_watchdog)
    fence_reached = asyncio.Event()
    release_stop = asyncio.Event()
    original_request = device._request_tunnel_stop

    async def pause_after_fence(*args, **kwargs):
        plan = await original_request(*args, **kwargs)
        fence_reached.set()
        await release_stop.wait()
        return plan

    monkeypatch.setattr(device, "_request_tunnel_stop", pause_after_fence)
    stop_task = asyncio.create_task(
        device.wifi_tunnel_stop(device.WifiTunnelStopRequest(udid=udid)),
    )
    start_task: asyncio.Task | None = None
    try:
        await asyncio.wait_for(fence_reached.wait(), timeout=0.5)
        # The stop watermark is already recorded, but its old runner has not
        # been detached.  A later start must still install R2/C2/E2 rather
        # than returning already_running for doomed R1.
        start_task = asyncio.create_task(
            device.wifi_tunnel_start_and_connect(
                device.WifiTunnelStartRequest(
                    ip="192.0.2.13",
                    port=49152,
                    udid=udid,
                ),
            ),
        )
        result = await asyncio.wait_for(start_task, timeout=0.5)
        assert result["status"] == "connected"
        assert result["udid"] == udid
        assert result["port"] == 49152
        assert tunnel_manager._tunnels[udid] is new_runner
        assert dm._connections[udid].name == "C2"
        assert state.simulation_engines[udid] is new_engine

        release_stop.set()
        stop_result = await asyncio.wait_for(stop_task, timeout=0.5)
        assert stop_result["status"] == "stopped"
        assert tunnel_manager._tunnels[udid] is new_runner
        assert dm._connections[udid].name == "C2"
        assert state.simulation_engines[udid] is new_engine
        assert not any(event == "device_disconnected" for event, _ in events)
    finally:
        release_stop.set()
        if start_task is not None and not start_task.done():
            start_task.cancel()
        if not stop_task.done():
            stop_task.cancel()
        await asyncio.gather(start_task, stop_task, return_exceptions=True)
        runner, watchdog, side_effects = await tunnel_manager._detach_tunnel(udid)
        await tunnel_manager._stop_tunnel_parts(
            runner,
            watchdog,
            caller="test_stop_linearized_teardown",
            udid=udid,
            side_effects=side_effects,
        )
        await old_runner.stop()


async def test_disconnect_stop_plan_cannot_remove_new_connection_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disconnect's old stop plan must preserve a concurrent C2/E2 start."""

    udid = "udid-1"
    temp_key = "pending:192.0.2.14:49152"
    dm = _LeaseDeviceManager(udid)
    _patch_device_manager(monkeypatch, dm)
    events = _patch_broadcast(monkeypatch)
    old_engine = _Engine("E1")
    new_engine = _Engine("E2")

    async def create_engine(_udid: str) -> None:
        state.simulation_engines[udid] = new_engine

    state = _patch_state(monkeypatch, create_engine)
    state.simulation_engines[udid] = old_engine
    state._primary_udid = udid
    dm.connect_count = 1
    dm._connections[udid] = _Connection("C1")

    old_runner = _Runner("R1")
    await old_runner.start(udid, "192.0.2.14", 49152)
    old_watchdog = asyncio.create_task(asyncio.Event().wait())
    tunnel_manager._tunnels[udid] = old_runner  # type: ignore[assignment]
    tunnel_manager._tunnel_watchdogs[udid] = old_watchdog
    tunnel_manager._tunnel_generations[udid] = 1

    new_runner = _Runner("R2")
    monkeypatch.setattr(device, "TunnelRunner", lambda: new_runner)
    monkeypatch.setattr(
        device,
        "_build_tunnel_udid_candidates",
        lambda _req: [temp_key],
    )

    async def idle_watchdog(*_args) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(device, "_per_tunnel_watchdog", idle_watchdog)
    fence_reached = asyncio.Event()
    release_disconnect = asyncio.Event()
    original_request = device._request_tunnel_stop

    async def pause_after_fence(*args, **kwargs):
        plan = await original_request(*args, **kwargs)
        fence_reached.set()
        await release_disconnect.wait()
        return plan

    monkeypatch.setattr(device, "_request_tunnel_stop", pause_after_fence)
    disconnect_task = asyncio.create_task(device.disconnect_device(udid))
    start_task: asyncio.Task | None = None
    try:
        await asyncio.wait_for(fence_reached.wait(), timeout=0.5)
        start_task = asyncio.create_task(
            device.wifi_tunnel_start_and_connect(
                device.WifiTunnelStartRequest(
                    ip="192.0.2.14",
                    port=49152,
                    udid=udid,
                ),
            ),
        )
        result = await asyncio.wait_for(start_task, timeout=0.5)
        assert result["status"] == "connected"
        assert tunnel_manager._tunnels[udid] is new_runner
        assert dm._connections[udid].name == "C2"
        assert state.simulation_engines[udid] is new_engine

        release_disconnect.set()
        disconnect_result = await asyncio.wait_for(disconnect_task, timeout=0.5)
        assert disconnect_result == {"status": "disconnected", "udid": udid}
        assert tunnel_manager._tunnels[udid] is new_runner
        assert dm._connections[udid].name == "C2"
        assert state.simulation_engines[udid] is new_engine
        assert not any(event == "device_disconnected" for event, _ in events)
    finally:
        release_disconnect.set()
        if start_task is not None and not start_task.done():
            start_task.cancel()
        if not disconnect_task.done():
            disconnect_task.cancel()
        await asyncio.gather(start_task, disconnect_task, return_exceptions=True)
        runner, watchdog, side_effects = await tunnel_manager._detach_tunnel(udid)
        await tunnel_manager._stop_tunnel_parts(
            runner,
            watchdog,
            caller="test_disconnect_generation_teardown",
            udid=udid,
            side_effects=side_effects,
        )
        await old_runner.stop()


async def test_start_and_connect_watchdog_owns_exact_lease_until_runner_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same-key route quiesces stale USB E0 before installing C1/E1."""

    udid = "udid-1"
    sibling_udid = "udid-sibling"
    dm = _LeaseDeviceManager(udid)
    usb_connection = _Connection("C0")
    usb_connection.connection_type = "USB"
    dm._connections[udid] = usb_connection
    sibling_connection = _Connection("CS")
    dm._connections[sibling_udid] = sibling_connection
    _patch_device_manager(monkeypatch, dm)
    events = _patch_broadcast(monkeypatch)
    old_engine = _BlockingQuiesceEngineWithStop("E0")
    old_engine._active_task = asyncio.create_task(old_engine.run_active())
    engine = _Engine("E1")
    sibling_engine = _Engine("ES")

    async def create_engine(_udid: str) -> None:
        # create_engine_for_device is idempotent; the route must remove the
        # stale USB identity before asking it to build the WiFi E1 engine.
        if _udid not in state.simulation_engines:
            state.simulation_engines[udid] = engine

    state = _patch_state(monkeypatch, create_engine)
    state.simulation_engines[udid] = old_engine
    state.simulation_engines[sibling_udid] = sibling_engine
    state._primary_udid = udid
    await asyncio.wait_for(old_engine.active_started.wait(), timeout=0.5)
    captured: dict[str, object] = {}
    captured_calls: list[dict[str, object]] = []
    watchdog_started = asyncio.Event()
    monkeypatch.setattr(device, "TunnelRunner", lambda: _Runner("R1"))
    monkeypatch.setattr(
        device,
        "_build_tunnel_udid_candidates",
        lambda _req: ["pending:192.0.2.20:49152"],
    )

    original_watchdog = tunnel_manager._per_tunnel_watchdog

    async def capture_watchdog(*args, **kwargs) -> None:
        call = {"args": args, **kwargs}
        captured_calls.append(call)
        if kwargs.get("connection_lease") is not None:
            captured.update(call)
            watchdog_started.set()
        await original_watchdog(*args, **kwargs)

    monkeypatch.setattr(device, "_per_tunnel_watchdog", capture_watchdog)
    monkeypatch.setattr(tunnel_manager, "_TUNNEL_RESTART_BACKOFF", ())

    async def no_fallback(_ip: str) -> list[tuple[str, int]]:
        return []

    monkeypatch.setattr(tunnel_manager, "find_fallback_endpoints", no_fallback)

    start_connect = asyncio.create_task(
        device.wifi_tunnel_start_and_connect(
            device.WifiTunnelStartRequest(ip="192.0.2.20", port=49152),
        ),
    )
    await asyncio.wait_for(old_engine.cancel_seen.wait(), timeout=0.5)
    assert not start_connect.done()
    assert old_engine.stop_started.is_set()
    assert not old_engine.stop_completed.is_set()
    assert state.simulation_engines[udid] is old_engine
    assert engine not in state.simulation_engines.values()
    old_engine.release_drain.set()
    result = await asyncio.wait_for(start_connect, timeout=0.5)
    assert result["status"] == "connected"
    assert result["udid"] == udid
    assert dm._connections[udid].name == "C1"
    assert state.simulation_engines[udid] is engine
    assert state._primary_udid == udid
    assert dm._connections[sibling_udid] is sibling_connection
    assert state.simulation_engines[sibling_udid] is sibling_engine
    assert old_engine._stop_event.is_set()
    assert old_engine._pause_event.is_set()
    await asyncio.wait_for(old_engine.active_done.wait(), timeout=0.5)
    assert old_engine._active_task is not None and old_engine._active_task.done()
    assert old_engine.stop_started.is_set()
    assert old_engine.stop_completed.is_set()

    await asyncio.wait_for(watchdog_started.wait(), timeout=0.5)
    assert any(call.get("connection_lease") is not None for call in captured_calls)
    runner = tunnel_manager._tunnels[udid]
    watchdog = tunnel_manager._tunnel_watchdogs[udid]
    assert captured["connection_lease"] is dm._connections[udid]
    assert captured["connection_engine"] is engine

    # Let the owned child die; the no-backoff watchdog then takes the exact
    # C1/E1 lease through final cleanup.
    runner._hold.set()  # type: ignore[attr-defined]
    await asyncio.wait_for(watchdog, timeout=0.5)

    assert runner.task is not None and runner.task.done()
    assert dm._connections == {sibling_udid: sibling_connection}
    assert [item[1].name for item in dm.close_calls] == ["C1"]
    assert state.simulation_engines == {sibling_udid: sibling_engine}
    assert state._primary_udid == sibling_udid
    assert "device_disconnected" in [event for event, _ in events]
    assert udid not in tunnel_manager._tunnels


async def test_start_and_connect_already_running_rebinds_without_borrowed_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An existing same-endpoint runner is borrowed, not owned by this start."""

    udid = "udid-1"
    dm = _LeaseDeviceManager(udid)
    _patch_device_manager(monkeypatch, dm)
    state = _patch_state(monkeypatch, lambda _udid: None)
    engine = _Engine("E1")

    async def create_engine(_udid: str) -> None:
        state.simulation_engines[udid] = engine

    monkeypatch.setattr(state, "create_engine_for_device", create_engine)
    monkeypatch.setattr(
        device,
        "_build_tunnel_udid_candidates",
        lambda _req: [udid],
    )
    borrowed = _Runner("borrowed")
    await borrowed.start(udid, "192.0.2.31", 49152)
    old_watchdog = asyncio.create_task(asyncio.Event().wait())
    tunnel_manager._tunnels[udid] = borrowed  # type: ignore[assignment]
    tunnel_manager._tunnel_watchdogs[udid] = old_watchdog
    tunnel_manager._tunnel_generations[udid] = 1

    def no_new_runner() -> _Runner:
        pytest.fail("already_running path must not construct a replacement runner")

    monkeypatch.setattr(device, "TunnelRunner", no_new_runner)

    async def idle_watchdog(*_args, **_kwargs) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(device, "_per_tunnel_watchdog", idle_watchdog)

    result = await device.wifi_tunnel_start_and_connect(
        device.WifiTunnelStartRequest(
            ip="192.0.2.31",
            port=49152,
            udid=udid,
        ),
    )

    assert result["status"] == "connected"
    assert result["udid"] == udid
    assert tunnel_manager._tunnels[udid] is borrowed
    assert borrowed.stop_calls == 0
    assert dm._connections[udid].name == "C1"
    assert state.simulation_engines[udid] is engine
    assert old_watchdog.done()

    runner, watchdog, side_effects = await tunnel_manager._detach_tunnel(udid)
    await tunnel_manager._stop_tunnel_parts(
        runner,
        watchdog,
        caller="test_already_running_teardown",
        udid=udid,
        side_effects=side_effects,
    )


async def test_start_and_connect_auto_syncs_new_wifi_follower(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A newly connected WiFi phone must join the active primary movement."""

    follower_udid = "pauline-follower"
    primary_udid = "pauline-primary"
    dm = _LeaseDeviceManager(follower_udid)
    _patch_device_manager(monkeypatch, dm)

    follower_engine = _Engine("E-follower")

    async def create_engine(udid: str) -> None:
        state.simulation_engines[udid] = follower_engine

    state = _patch_state(monkeypatch, create_engine)
    state.simulation_engines[primary_udid] = _Engine("E-primary")
    state._primary_udid = primary_udid

    monkeypatch.setattr(
        device,
        "_build_tunnel_udid_candidates",
        lambda _req: [follower_udid],
    )
    runner = _Runner("R-follower")
    monkeypatch.setattr(device, "TunnelRunner", lambda: runner)

    async def idle_watchdog(*_args, **_kwargs) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(device, "_per_tunnel_watchdog", idle_watchdog)

    synced: list[str] = []

    async def auto_sync(udid: str) -> None:
        synced.append(udid)

    monkeypatch.setattr("main._auto_sync_new_device_to_primary", auto_sync)

    result = await device.wifi_tunnel_start_and_connect(
        device.WifiTunnelStartRequest(
            ip="192.0.2.32",
            port=49152,
            udid=follower_udid,
        ),
    )

    assert result["status"] == "connected"
    assert result["udid"] == follower_udid
    assert synced == [follower_udid]

    owned_runner, watchdog, side_effects = await tunnel_manager._detach_tunnel(
        follower_udid,
    )
    await tunnel_manager._stop_tunnel_parts(
        owned_runner,
        watchdog,
        caller="test_auto_sync_teardown",
        udid=follower_udid,
        side_effects=side_effects,
    )


async def test_start_and_connect_reuses_existing_connection_and_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An offline pin retry must not rebuild an already-connected primary."""

    primary_udid = "pauline-primary"
    offline_hint = "sleeping-follower"
    dm = _LeaseDeviceManager(primary_udid)
    existing_connection = _Connection("C0")
    dm._connections[primary_udid] = existing_connection
    _patch_device_manager(monkeypatch, dm)

    state = _patch_state(monkeypatch, lambda _udid: None)
    existing_engine = _Engine("E0")
    state.simulation_engines[primary_udid] = existing_engine
    state._primary_udid = primary_udid

    monkeypatch.setattr(
        device,
        "_build_tunnel_udid_candidates",
        lambda _req: [primary_udid],
    )
    borrowed = _Runner("borrowed-primary")
    await borrowed.start(primary_udid, "192.0.2.31", 49152)
    old_watchdog = asyncio.create_task(asyncio.Event().wait())
    tunnel_manager._tunnels[primary_udid] = borrowed  # type: ignore[assignment]
    tunnel_manager._tunnel_watchdogs[primary_udid] = old_watchdog
    tunnel_manager._tunnel_generations[primary_udid] = 1

    result = await device.wifi_tunnel_start_and_connect(
        device.WifiTunnelStartRequest(
            ip="192.0.2.31",
            port=49152,
            udid=offline_hint,
        ),
    )

    assert result["status"] == "connected"
    assert result["udid"] == primary_udid
    assert dm.connect_count == 0
    assert dm._connections[primary_udid] is existing_connection
    assert state.simulation_engines[primary_udid] is existing_engine
    assert state._primary_udid == primary_udid
    assert tunnel_manager._tunnels[primary_udid] is borrowed
    assert borrowed.stop_calls == 0
    assert not old_watchdog.done()

    runner, watchdog, side_effects = await tunnel_manager._detach_tunnel(primary_udid)
    await tunnel_manager._stop_tunnel_parts(
        runner,
        watchdog,
        caller="test_existing_connection_teardown",
        udid=primary_udid,
        side_effects=side_effects,
    )


async def test_start_and_connect_engine_exception_cleans_owned_resources_and_returns_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed legacy start-and-connect engine setup leaves no C1/R1/E1."""

    udid = "udid-1"
    temp_key = "pending:192.0.2.41:49152"
    dm = _LeaseDeviceManager(udid)
    _patch_device_manager(monkeypatch, dm)
    events = _patch_broadcast(monkeypatch)
    engine = _Engine("E1")

    async def fail_create_engine(_udid: str) -> None:
        state.simulation_engines[udid] = engine
        raise RuntimeError("engine setup failed")

    state = _patch_state(monkeypatch, fail_create_engine)
    monkeypatch.setattr(
        device,
        "_build_tunnel_udid_candidates",
        lambda _req: [temp_key],
    )
    runner = _Runner("R1")
    monkeypatch.setattr(device, "TunnelRunner", lambda: runner)

    async def idle_watchdog(*_args, **_kwargs) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(device, "_per_tunnel_watchdog", idle_watchdog)

    with pytest.raises(HTTPException) as caught:
        await device.wifi_tunnel_start_and_connect(
            device.WifiTunnelStartRequest(ip="192.0.2.41", port=49152),
        )

    assert caught.value.status_code == 500
    assert dm._connections == {}
    assert [item[1].name for item in dm.close_calls] == ["C1"]
    assert state.simulation_engines == {}
    assert runner.stop_calls >= 1
    assert tunnel_manager._tunnels == {}
    assert tunnel_manager._tunnel_watchdogs == {}
    assert [event for event, _ in events].count("device_disconnected") == 1


async def test_legacy_wifi_tunnel_engine_exception_cleans_owned_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The legacy /wifi/tunnel route rolls back C1/E1 on engine failure."""

    udid = "udid-1"
    dm = _LeaseDeviceManager(udid)
    _patch_device_manager(monkeypatch, dm)
    events = _patch_broadcast(monkeypatch)
    engine = _Engine("E1")

    async def fail_create_engine(_udid: str) -> None:
        state.simulation_engines[udid] = engine
        raise RuntimeError("engine setup failed")

    state = _patch_state(monkeypatch, fail_create_engine)

    with pytest.raises(HTTPException) as caught:
        await device.wifi_tunnel_connect(
            device.WifiTunnelConnectRequest(
                rsd_address="fd00::R1",
                rsd_port=12345,
            ),
        )

    assert caught.value.status_code == 500
    assert dm._connections == {}
    assert [item[1].name for item in dm.close_calls] == ["C1"]
    assert state.simulation_engines == {}
    assert [event for event, _ in events].count("device_disconnected") == 1


async def test_same_endpoint_adoption_waits_for_owner_failure_and_preserves_new_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A borrower cannot adopt R1 while its owner is still in post-setup."""

    udid = "udid-1"
    dm = _LeaseDeviceManager(udid)
    _patch_device_manager(monkeypatch, dm)
    events = _patch_broadcast(monkeypatch)
    first_engine_started = asyncio.Event()
    release_first_failure = asyncio.Event()
    second_engine_started = asyncio.Event()
    engines: list[_Engine] = []

    async def create_engine(_udid: str) -> None:
        if not engines:
            first = _Engine("E1")
            engines.append(first)
            state.simulation_engines[udid] = first
            first_engine_started.set()
            await release_first_failure.wait()
            raise RuntimeError("owner post-setup failed")
        second = _Engine("E2")
        engines.append(second)
        state.simulation_engines[udid] = second
        second_engine_started.set()

    state = _patch_state(monkeypatch, create_engine)
    monkeypatch.setattr(device, "_build_tunnel_udid_candidates", lambda _req: [udid])
    r1 = _Runner("R1")
    r2 = _Runner("R2")
    runners = [r1, r2]
    monkeypatch.setattr(device, "TunnelRunner", lambda: runners.pop(0))

    async def idle_watchdog(*_args, **_kwargs) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(device, "_per_tunnel_watchdog", idle_watchdog)

    owner = asyncio.create_task(
        device.wifi_tunnel_start_and_connect(
            device.WifiTunnelStartRequest(
                ip="192.0.2.42",
                port=49152,
                udid=udid,
            ),
        ),
    )
    borrower: asyncio.Task | None = None
    try:
        await asyncio.wait_for(first_engine_started.wait(), timeout=0.5)
        borrower = asyncio.create_task(
            device.wifi_tunnel_start_and_connect(
                device.WifiTunnelStartRequest(
                    ip="192.0.2.42",
                    port=49152,
                    udid=udid,
                ),
            ),
        )
        await asyncio.sleep(0.05)
        assert not second_engine_started.is_set(), (
            "same-endpoint start borrowed R1 before owner post-setup completed"
        )
        assert not borrower.done()

        release_first_failure.set()
        owner_result = await asyncio.gather(owner, return_exceptions=True)
        assert isinstance(owner_result[0], HTTPException)
        assert owner_result[0].status_code == 500

        result = await asyncio.wait_for(borrower, timeout=0.5)
        assert result["status"] == "connected"
        assert tunnel_manager._tunnels[udid] is r2
        assert r1.stop_calls >= 1
        assert r2.stop_calls == 0
        assert dm._connections[udid].name == "C2"
        assert state.simulation_engines[udid] is engines[-1]
        assert [event for event, _ in events].count("device_disconnected") == 1
    finally:
        release_first_failure.set()
        for task in (owner, borrower):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (owner, borrower) if task is not None),
            return_exceptions=True,
        )
        runner, watchdog, side_effects = await tunnel_manager._detach_tunnel(udid)
        await tunnel_manager._stop_tunnel_parts(
            runner,
            watchdog,
            caller="test_same_endpoint_adoption_teardown",
            udid=udid,
            side_effects=side_effects,
        )


async def test_restart_and_start_and_connect_share_adoption_lock_and_exact_final_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """API adoption waits for restart post-setup, then rebinds exact C2/E2."""

    udid = "udid-1"
    ip = "192.0.2.52"
    port = 49152
    dm = _LeaseDeviceManager(udid)
    _patch_device_manager(monkeypatch, dm)
    events = _patch_broadcast(monkeypatch)
    restart_engine_started = asyncio.Event()
    release_restart_engine = asyncio.Event()
    api_engine_started = asyncio.Event()
    created_engines: list[_Engine] = []

    async def create_engine(_udid: str) -> None:
        if not created_engines:
            engine = _Engine("E-restart")
            created_engines.append(engine)
            state.simulation_engines[udid] = engine
            restart_engine_started.set()
            await release_restart_engine.wait()
            return
        engine = _Engine("E-api")
        created_engines.append(engine)
        state.simulation_engines[udid] = engine
        api_engine_started.set()

    state = _patch_state(monkeypatch, create_engine)
    state._primary_udid = udid
    original = _Runner("R-original")
    replacement = _Runner("R-restart")
    tunnel_manager._tunnels[udid] = original  # type: ignore[assignment]
    tunnel_manager._tunnel_generations[udid] = 7
    monkeypatch.setattr(tunnel_manager, "TunnelRunner", lambda: replacement)
    monkeypatch.setattr(device, "_build_tunnel_udid_candidates", lambda _req: [udid])

    def no_second_runner() -> _Runner:
        pytest.fail("API must adopt the completed restart runner, not create R3")

    monkeypatch.setattr(device, "TunnelRunner", no_second_runner)
    watchdog_calls: list[tuple[tuple, dict]] = []

    async def idle_watchdog(*args, **kwargs) -> None:
        watchdog_calls.append((args, kwargs))
        await asyncio.Event().wait()

    monkeypatch.setattr(tunnel_manager, "_per_tunnel_watchdog", idle_watchdog)
    monkeypatch.setattr(device, "_per_tunnel_watchdog", idle_watchdog)

    async def no_auto_sync(_udid: str) -> None:
        return None

    monkeypatch.setattr("main._auto_sync_new_device_to_primary", no_auto_sync)

    restart_task = asyncio.create_task(
        tunnel_manager._attempt_tunnel_restart(
            udid,
            ip,
            port,
            None,
            original,
        ),
    )
    api_task: asyncio.Task | None = None
    try:
        await asyncio.wait_for(restart_engine_started.wait(), timeout=0.5)
        assert tunnel_manager._tunnels[udid] is replacement
        assert tunnel_manager._tunnel_generations[udid] == 8
        assert dm._connections[udid].name == "C1"

        api_task = asyncio.create_task(
            device.wifi_tunnel_start_and_connect(
                device.WifiTunnelStartRequest(
                    ip=ip,
                    port=port,
                    udid=udid,
                ),
            ),
        )
        await asyncio.sleep(0.05)
        assert not api_task.done(), "API adopted Rnew before restart post-setup completed"
        assert not api_engine_started.is_set()

        # Stop targets do not acquire the composite adoption lock. An
        # unrelated stop must remain responsive while restart/API are gated.
        stop_task = asyncio.create_task(
            device.wifi_tunnel_stop(device.WifiTunnelStopRequest(udid="other")),
        )
        stop_result = await asyncio.wait_for(stop_task, timeout=0.2)
        assert stop_result["status"] in {"stopped", "not_running"}

        release_restart_engine.set()
        assert await asyncio.wait_for(restart_task, timeout=0.5) is True
        result = await asyncio.wait_for(api_task, timeout=0.5)
        assert result["status"] == "connected"
        assert api_engine_started.is_set()

        final_runner = tunnel_manager._tunnels[udid]
        final_generation = tunnel_manager._tunnel_generations[udid]
        final_lease = dm._connections[udid]
        final_engine = state.simulation_engines[udid]
        assert final_runner is replacement
        assert final_generation > 8
        assert final_lease.name == "C2"
        assert final_engine is created_engines[-1]
        final_wd = tunnel_manager._tunnel_watchdogs[udid]
        matching = [
            (args, kwargs)
            for args, kwargs in watchdog_calls
            if len(args) >= 3 and args[1] is final_runner and args[2] == final_generation
        ]
        assert matching
        args, kwargs = matching[-1]
        assert args[1] is final_runner
        assert args[2] == final_generation
        assert kwargs["connection_lease"] is final_lease
        assert kwargs["connection_engine"] is final_engine
        assert not final_wd.done()
        assert replacement.stop_calls == 0
        assert original.stop_calls == 0
        assert not any(event == "device_disconnected" for event, _ in events)
    finally:
        release_restart_engine.set()
        for task in (restart_task, api_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (restart_task, api_task) if task is not None),
            return_exceptions=True,
        )
        runner, watchdog, side_effects = await tunnel_manager._detach_tunnel(udid)
        await tunnel_manager._stop_tunnel_parts(
            runner,
            watchdog,
            caller="test_restart_api_interleaving_teardown",
            udid=udid,
            side_effects=side_effects,
        )


async def _create_new_engine(state, engine: _Engine) -> None:
    state.simulation_engines["udid-1"] = engine


async def test_cancelled_old_start_and_connect_cannot_cleanup_new_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An old cancelled finally block must CAS-fail after C2/E2 replaces C1."""

    udid = "udid-1"
    temp_key = "pending:192.0.2.12:49152"
    dm = _LeaseDeviceManager(udid)
    _patch_device_manager(monkeypatch, dm)
    events = _patch_broadcast(monkeypatch)
    old_create_started = asyncio.Event()
    release_old_create = asyncio.Event()
    old_emit_started = asyncio.Event()
    release_old_emit = asyncio.Event()
    old_engine = _Engine(
        "E1",
        emit_started=old_emit_started,
        release_emit=release_old_emit,
    )
    new_engine = _Engine("E2")
    create_count = 0

    async def create_engine(_udid: str) -> None:
        nonlocal create_count
        create_count += 1
        if create_count == 1:
            state.simulation_engines[udid] = old_engine
            old_create_started.set()
            await release_old_create.wait()
        else:
            state.simulation_engines[udid] = new_engine

    state = _patch_state(monkeypatch, create_engine)
    state._primary_udid = udid
    monkeypatch.setattr(
        device,
        "_build_tunnel_udid_candidates",
        lambda _req: [temp_key],
    )
    runner_index = 0

    def make_runner() -> _Runner:
        nonlocal runner_index
        runner_index += 1
        return _Runner(f"R{runner_index}")

    monkeypatch.setattr(device, "TunnelRunner", make_runner)

    async def idle_watchdog(*_args) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(device, "_per_tunnel_watchdog", idle_watchdog)

    old_task = asyncio.create_task(
        device.wifi_tunnel_start_and_connect(
            device.WifiTunnelStartRequest(ip="192.0.2.12", port=49152),
        ),
    )
    new_task: asyncio.Task | None = None
    try:
        await asyncio.wait_for(old_create_started.wait(), timeout=0.5)
        assert dm._connections[udid].name == "C1"
        old_task.cancel()
        # A second cancellation must not interrupt the finish-start cleanup
        # before it drains C1/E1 and releases the old runner lease.
        old_task.cancel()
        await asyncio.wait_for(old_emit_started.wait(), timeout=0.5)

        new_task = asyncio.create_task(
            device.wifi_tunnel_start_and_connect(
                device.WifiTunnelStartRequest(ip="192.0.2.12", port=49152),
            ),
        )
        # Composite starts are serialized for the whole owner lifecycle.
        # B must not borrow R1 while A's cancellation cleanup is still
        # blocked in its engine emit.
        await asyncio.sleep(0.05)
        assert not new_task.done()

        release_old_emit.set()
        release_old_create.set()
        old_result = await asyncio.gather(old_task, return_exceptions=True)
        assert isinstance(old_result[0], asyncio.CancelledError)

        new_result = await asyncio.wait_for(new_task, timeout=0.5)
        assert new_result["status"] == "connected"
        assert dm._connections[udid].name == "C2"
        assert state.simulation_engines[udid] is new_engine
        assert dm._connections[udid].name == "C2"
        assert state.simulation_engines[udid] is new_engine
        assert [event for event, _ in events].count("device_disconnected") == 1
        assert tunnel_manager._tunnels[udid] is not None
    finally:
        release_old_create.set()
        release_old_emit.set()
        if new_task is not None and not new_task.done():
            new_task.cancel()
        if not old_task.done():
            old_task.cancel()
        tasks = [task for task in (old_task, new_task) if task is not None]
        await asyncio.gather(*tasks, return_exceptions=True)
        runner, watchdog, side_effects = await tunnel_manager._detach_tunnel(udid)
        await tunnel_manager._stop_tunnel_parts(
            runner,
            watchdog,
            caller="test_cancelled_start_teardown",
            udid=udid,
            side_effects=side_effects,
        )


async def test_cleanup_mismatched_connection_lease_is_a_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale cleanup may not disconnect/pop the replacement connection."""

    udid = "udid-1"
    dm = _LeaseDeviceManager(udid)
    _patch_device_manager(monkeypatch, dm)
    events = _patch_broadcast(monkeypatch)
    detach_started = asyncio.Event()
    release_detach = asyncio.Event()
    old_engine = _Engine("E1")
    new_engine = _Engine("E2")

    async def create_engine(_udid: str) -> None:
        return None

    state = _patch_state(monkeypatch, create_engine)
    state.simulation_engines[udid] = old_engine
    state._primary_udid = udid
    old_connection = _Connection("C1")
    new_connection = _Connection("C2")
    dm._connections[udid] = old_connection

    original_detach = dm._detach_connection

    async def paused_detach(
        target_udid: str,
        *,
        expected: _Connection | None = None,
    ) -> _Connection | None:
        detach_started.set()
        await release_detach.wait()
        return await original_detach(target_udid, expected=expected)

    monkeypatch.setattr(dm, "_detach_connection", paused_detach)

    cleanup_task = asyncio.create_task(
        tunnel_manager._cleanup_wifi_connection_for(
            udid,
            caller="test_stale_lease",
            expected_connection=old_connection,
            # Explicit None is a sentinel: this stale lease has no engine
            # ownership and must not snapshot/stop the replacement E2.
            expected_engine=None,
        ),
    )
    try:
        await asyncio.wait_for(detach_started.wait(), timeout=0.5)
        dm._connections[udid] = new_connection
        state.simulation_engines[udid] = new_engine
        release_detach.set()
        await asyncio.wait_for(cleanup_task, timeout=0.5)
    finally:
        release_detach.set()
        if not cleanup_task.done():
            cleanup_task.cancel()
        await asyncio.gather(cleanup_task, return_exceptions=True)

    assert dm.disconnect_calls == []
    assert dm.close_calls == []
    assert dm._connections[udid] is new_connection
    assert state.simulation_engines[udid] is new_engine
    assert state._primary_udid == udid
    assert not events


async def test_old_engine_stop_plan_without_c1_preserves_c2_and_e2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A detached E1-only plan must not infer or detach a newer C2 lease."""

    udid = "udid-1"
    dm = _LeaseDeviceManager(udid)
    _patch_device_manager(monkeypatch, dm)
    events = _patch_broadcast(monkeypatch)
    old_engine = _Engine("E1")
    old_engine._active_task = asyncio.create_task(old_engine.run_active())
    new_engine = _Engine("E2")
    state = _patch_state(monkeypatch, lambda _udid: None)
    state.simulation_engines[udid] = new_engine
    state._primary_udid = udid
    dm._connections[udid] = _Connection("C2")
    old_runner = _Runner("R1")
    await old_runner.start(udid, "192.0.2.30", 49152)

    plan = tunnel_manager.TunnelStopPlan(
        target_udid=udid,
        udids=(udid,),
        parts=(
            tunnel_manager.TunnelStopPart(
                udid=udid,
                runner=old_runner,
                expected_engine=old_engine,
            ),
        ),
        pending_tasks=(),
        matched_pending=False,
    )

    await tunnel_manager._stop_tunnel_plan(plan, caller="test_old_engine_plan")

    assert old_runner.stop_calls == 1
    assert old_engine._stop_event.is_set()
    await asyncio.wait_for(old_engine.active_done.wait(), timeout=0.5)
    assert dm._connections[udid].name == "C2"
    assert state.simulation_engines[udid] is new_engine
    assert dm.close_calls == []
    assert dm.disconnect_calls == []
    assert not events


async def test_cleanup_without_replacement_disconnects_and_broadcasts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ordinary cleanup with its expected connection still current runs."""

    udid = "udid-1"
    dm = _LeaseDeviceManager(udid)
    _patch_device_manager(monkeypatch, dm)
    events = _patch_broadcast(monkeypatch)
    engine = _Engine("E1")

    async def create_engine(_udid: str) -> None:
        return None

    state = _patch_state(monkeypatch, create_engine)
    state.simulation_engines[udid] = engine
    state._primary_udid = udid
    dm._connections[udid] = _Connection("C1")

    result = await tunnel_manager._cleanup_wifi_connection_for(
        udid,
        caller="test_ordinary_cleanup",
    )

    assert result is True
    assert dm.disconnect_calls == []
    assert dm.close_calls == [(udid, dm.close_calls[0][1])]
    assert udid not in dm._connections
    assert udid not in state.simulation_engines
    assert state._primary_udid is None
    assert [event for event, _ in events] == ["device_disconnected"]

    # A finalizer racing a completed ordinary disconnect must be idempotent:
    # it cannot emit a second event or invoke the manager close path again.
    assert await tunnel_manager._cleanup_wifi_connection_for(
        udid,
        caller="test_ordinary_cleanup_repeat",
    ) is False
    assert dm.disconnect_calls == []
    assert dm.close_calls == [(udid, dm.close_calls[0][1])]
    assert [event for event, _ in events] == ["device_disconnected"]


async def test_device_manager_connection_lease_cas_preserves_replacement() -> None:
    """The frozen DM lease primitive rejects stale expected connections."""

    from core.device_manager import DeviceManager, _ActiveConnection

    udid = "udid-1"
    manager = DeviceManager()
    old = _ActiveConnection(udid, object(), "17.5", connection_type="Network")
    replacement = _ActiveConnection(
        udid,
        object(),
        "17.5",
        connection_type="Network",
    )
    manager._connections[udid] = replacement

    assert await manager.disconnect_if_current(udid, old) is False
    assert manager._connections[udid] is replacement

    assert await manager.disconnect_if_current(udid, replacement) is True
    assert udid not in manager._connections


async def test_cancelled_connection_cleanup_drains_detached_lease_then_reraises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation during engine teardown cannot strand the detached lease."""

    udid = "udid-1"
    dm = _LeaseDeviceManager(udid)
    _patch_device_manager(monkeypatch, dm)
    events = _patch_broadcast(monkeypatch)
    emit_started = asyncio.Event()
    release_emit = asyncio.Event()
    engine = _Engine("E1", emit_started=emit_started, release_emit=release_emit)
    connection = _Connection("C1")
    dm._connections[udid] = connection

    async def create_engine(_udid: str) -> None:
        return None

    state = _patch_state(monkeypatch, create_engine)
    state.simulation_engines[udid] = engine
    state._primary_udid = udid
    cleanup = asyncio.create_task(
        tunnel_manager._cleanup_wifi_connection_for(
            udid,
            caller="test_cancelled_connection_cleanup",
            expected_connection=connection,
        ),
    )
    try:
        await asyncio.wait_for(emit_started.wait(), timeout=0.5)
        # The CAS has already detached C1 at this point.  Cancellation must
        # be remembered while close/pop/broadcast finish, then re-raised.
        cleanup.cancel()
        release_emit.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(cleanup, timeout=0.5)
    finally:
        release_emit.set()
        if not cleanup.done():
            cleanup.cancel()
        await asyncio.gather(cleanup, return_exceptions=True)

    assert udid not in dm._connections
    assert [item[1].name for item in dm.close_calls] == ["C1"]
    assert udid not in state.simulation_engines
    assert state._primary_udid is None
    assert [event for event, _ in events] == ["device_disconnected"]


async def test_cleanup_wifi_connection_awaits_engine_stop_hook_drain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A production stop hook must finish its active-task drain before return."""

    udid = "udid-quiesce-stop"
    dm = _LeaseDeviceManager(udid)
    connection = _Connection("C1")
    dm._connections[udid] = connection
    _patch_device_manager(monkeypatch, dm)
    events = _patch_broadcast(monkeypatch)
    engine = _BlockingQuiesceEngineWithStop("E1")
    engine._active_task = asyncio.create_task(engine.run_active())

    state = _patch_state(monkeypatch, lambda _udid: None)
    state.simulation_engines[udid] = engine
    await asyncio.wait_for(engine.active_started.wait(), timeout=0.5)

    cleanup = asyncio.create_task(
        tunnel_manager._cleanup_wifi_connection_for(
            udid,
            caller="test_engine_stop_hook_drain",
            expected_connection=connection,
            expected_engine=engine,
        ),
    )
    await asyncio.wait_for(engine.cancel_seen.wait(), timeout=0.5)
    assert engine.stop_started.is_set()
    assert not engine.stop_completed.is_set()
    assert not cleanup.done()

    engine.release_drain.set()
    assert await asyncio.wait_for(cleanup, timeout=0.5) is True
    assert engine.stop_completed.is_set()
    assert engine.active_done.is_set()
    assert engine._active_task is not None and engine._active_task.done()
    assert dm._connections == {}
    assert [item[1].name for item in dm.close_calls] == ["C1"]
    assert state.simulation_engines == {}
    assert [event for event, _ in events].count("device_disconnected") == 1


async def test_cleanup_wifi_connection_awaits_active_task_without_stop_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The compatibility fallback must also drain an engine with no stop()."""

    udid = "udid-quiesce-fallback"
    dm = _LeaseDeviceManager(udid)
    connection = _Connection("C1")
    dm._connections[udid] = connection
    _patch_device_manager(monkeypatch, dm)
    events = _patch_broadcast(monkeypatch)
    engine = _BlockingQuiesceEngine("E1")
    engine._active_task = asyncio.create_task(engine.run_active())

    state = _patch_state(monkeypatch, lambda _udid: None)
    state.simulation_engines[udid] = engine
    await asyncio.wait_for(engine.active_started.wait(), timeout=0.5)

    cleanup = asyncio.create_task(
        tunnel_manager._cleanup_wifi_connection_for(
            udid,
            caller="test_engine_active_fallback_drain",
            expected_connection=connection,
            expected_engine=engine,
        ),
    )
    await asyncio.wait_for(engine.cancel_seen.wait(), timeout=0.5)
    assert not cleanup.done()

    engine.release_drain.set()
    assert await asyncio.wait_for(cleanup, timeout=0.5) is True
    assert engine.active_done.is_set()
    assert engine._active_task is not None and engine._active_task.done()
    assert dm._connections == {}
    assert [item[1].name for item in dm.close_calls] == ["C1"]
    assert state.simulation_engines == {}
    assert [event for event, _ in events].count("device_disconnected") == 1


async def test_primitive_start_then_connect_rearms_exact_lease_watchdog_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two primitive routes must compose into one owned lifecycle.

    ``/wifi/tunnel/start`` first publishes a lease-less runner.  Connecting
    the returned RSD endpoint must replace that observer with one carrying
    the exact C1/E1 identities; when the runner exits, only that owned
    lifecycle is removed and one ``tunnel_lost`` event is emitted.
    """

    udid = "udid-primitive"
    ip = "192.0.2.61"
    port = 49152
    dm = _LeaseDeviceManager(udid)
    _patch_device_manager(monkeypatch, dm)
    events = _patch_broadcast(monkeypatch)
    engine = _Engine("E1")

    async def create_engine(_udid: str) -> None:
        state.simulation_engines[udid] = engine

    state = _patch_state(monkeypatch, create_engine)
    runner = _Runner("R1")
    monkeypatch.setattr(device, "TunnelRunner", lambda: runner)
    monkeypatch.setattr(
        device,
        "_build_tunnel_udid_candidates",
        lambda _req: [udid],
    )

    observed: list[tuple[tuple, dict]] = []
    rearmed = asyncio.Event()
    original_watchdog = tunnel_manager._per_tunnel_watchdog

    async def observe_watchdog(*args, **kwargs) -> None:
        observed.append((args, kwargs))
        if kwargs.get("connection_lease") is not None:
            rearmed.set()
        await original_watchdog(*args, **kwargs)

    monkeypatch.setattr(device, "_per_tunnel_watchdog", observe_watchdog)
    monkeypatch.setattr(tunnel_manager, "_TUNNEL_RESTART_BACKOFF", ())

    async def no_fallback(_ip: str) -> list[tuple[str, int]]:
        return []

    monkeypatch.setattr(tunnel_manager, "find_fallback_endpoints", no_fallback)

    started = await device.wifi_tunnel_start(
        device.WifiTunnelStartRequest(
            ip=ip,
            port=port,
            udid=udid,
        ),
    )
    assert started["status"] == "started"
    assert started["udid"] == udid
    assert started["port"] == port
    assert tunnel_manager._tunnels[udid] is runner

    connected = await device.wifi_tunnel_connect(
        device.WifiTunnelConnectRequest(
            rsd_address=started["rsd_address"],
            rsd_port=started["rsd_port"],
        ),
    )
    assert connected["status"] == "connected"
    assert connected["udid"] == udid
    assert dm._connections[udid].name == "C1"
    assert state.simulation_engines[udid] is engine

    await asyncio.wait_for(rearmed.wait(), timeout=0.5)
    matching = [
        (args, kwargs)
        for args, kwargs in observed
        if kwargs.get("connection_lease") is dm._connections[udid]
    ]
    assert matching
    args, kwargs = matching[-1]
    assert args[0] == udid
    assert args[1] is runner
    assert kwargs["connection_lease"] is dm._connections[udid]
    assert kwargs["connection_engine"] is engine

    # Force the long-lived runner task to exit.  With no retry backoff the
    # matching watchdog performs final cleanup immediately.
    watchdog = tunnel_manager._tunnel_watchdogs[udid]
    runner._hold.set()
    await asyncio.wait_for(watchdog, timeout=0.5)

    assert runner.task is not None and runner.task.done()
    assert udid not in tunnel_manager._tunnels
    assert udid not in tunnel_manager._tunnel_watchdogs
    assert dm._connections == {}
    assert [item[1].name for item in dm.close_calls] == ["C1"]
    assert state.simulation_engines == {}
    assert [event for event, _ in events].count("tunnel_lost") == 1
    assert [
        data
        for event, data in events
        if event == "tunnel_lost"
    ] == [{"udid": udid, "reason": "task_exited"}]
    assert [event for event, _ in events].count("device_disconnected") == 1


async def test_direct_connect_rebuilds_engine_after_usb_to_wifi_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct connect must replace stale USB engine state before rearming WD."""

    udid = "udid-usb-wifi"
    ip = "192.0.2.63"
    port = 49152
    dm = _LeaseDeviceManager(udid)
    usb_connection = _Connection("C0")
    usb_connection.connection_type = "USB"
    dm._connections[udid] = usb_connection
    sibling_udid = "udid-usb-sibling"
    sibling_connection = _Connection("CS")
    dm._connections[sibling_udid] = sibling_connection
    _patch_device_manager(monkeypatch, dm)
    events = _patch_broadcast(monkeypatch)
    old_engine = _BlockingQuiesceEngineWithStop("E0")
    old_engine._active_task = asyncio.create_task(old_engine.run_active())
    new_engine = _Engine("E1")
    sibling_engine = _Engine("ES")
    create_calls: list[str] = []

    async def create_engine(device_udid: str) -> None:
        create_calls.append(device_udid)
        # This fake models the idempotent app-state helper: callers must drop
        # a stale engine before asking for a new RSD-bound engine.
        if device_udid not in state.simulation_engines:
            state.simulation_engines[device_udid] = new_engine

    state = _patch_state(monkeypatch, create_engine)
    state.simulation_engines[udid] = old_engine
    state.simulation_engines[sibling_udid] = sibling_engine
    state._primary_udid = udid
    await asyncio.wait_for(old_engine.active_started.wait(), timeout=0.5)
    runner = _Runner("R-usb-wifi")
    monkeypatch.setattr(device, "TunnelRunner", lambda: runner)
    monkeypatch.setattr(
        device,
        "_build_tunnel_udid_candidates",
        lambda _req: [udid],
    )

    observed: list[tuple[tuple, dict]] = []
    rearmed = asyncio.Event()
    original_watchdog = tunnel_manager._per_tunnel_watchdog

    async def observe_watchdog(*args, **kwargs) -> None:
        observed.append((args, kwargs))
        if kwargs.get("connection_lease") is not None:
            rearmed.set()
        await original_watchdog(*args, **kwargs)

    monkeypatch.setattr(device, "_per_tunnel_watchdog", observe_watchdog)
    monkeypatch.setattr(tunnel_manager, "_TUNNEL_RESTART_BACKOFF", ())

    async def no_fallback(_ip: str) -> list[tuple[str, int]]:
        return []

    monkeypatch.setattr(tunnel_manager, "find_fallback_endpoints", no_fallback)

    started = await device.wifi_tunnel_start(
        device.WifiTunnelStartRequest(
            ip=ip,
            port=port,
            udid=udid,
        ),
    )
    assert started["status"] == "started"
    connect_task = asyncio.create_task(
        device.wifi_tunnel_connect(
            device.WifiTunnelConnectRequest(
                rsd_address=started["rsd_address"],
                rsd_port=started["rsd_port"],
            ),
        ),
    )
    await asyncio.wait_for(old_engine.cancel_seen.wait(), timeout=0.5)
    assert not connect_task.done()
    assert old_engine.stop_started.is_set()
    assert not old_engine.stop_completed.is_set()
    assert state.simulation_engines[udid] is old_engine
    assert new_engine not in state.simulation_engines.values()
    old_engine.release_drain.set()
    connected = await asyncio.wait_for(connect_task, timeout=0.5)
    assert connected["status"] == "connected"
    assert connected["udid"] == udid
    assert dm._connections[udid].name == "C1"
    assert create_calls == [udid]
    assert state.simulation_engines[udid] is new_engine
    assert old_engine not in state.simulation_engines.values()
    assert state._primary_udid == udid
    assert dm._connections[sibling_udid] is sibling_connection
    assert state.simulation_engines[sibling_udid] is sibling_engine
    assert old_engine._stop_event.is_set()
    assert old_engine._pause_event.is_set()
    await asyncio.wait_for(old_engine.active_done.wait(), timeout=0.5)
    assert old_engine._active_task is not None and old_engine._active_task.done()
    assert old_engine.stop_started.is_set()
    assert old_engine.stop_completed.is_set()

    await asyncio.wait_for(rearmed.wait(), timeout=0.5)
    matching = [
        (args, kwargs)
        for args, kwargs in observed
        if kwargs.get("connection_lease") is dm._connections[udid]
    ]
    assert matching
    args, kwargs = matching[-1]
    assert args[0] == udid
    assert args[1] is runner
    assert kwargs["connection_lease"] is dm._connections[udid]
    assert kwargs["connection_engine"] is new_engine

    watchdog = tunnel_manager._tunnel_watchdogs[udid]
    runner._hold.set()
    await asyncio.wait_for(watchdog, timeout=0.5)
    assert runner.task is not None and runner.task.done()
    assert udid not in tunnel_manager._tunnels
    assert udid not in tunnel_manager._tunnel_watchdogs
    assert dm._connections == {sibling_udid: sibling_connection}
    assert [item[1].name for item in dm.close_calls] == ["C1"]
    assert state.simulation_engines == {sibling_udid: sibling_engine}
    assert state._primary_udid == sibling_udid
    assert [event for event, _ in events].count("tunnel_lost") == 1
    assert [event for event, _ in events].count("device_disconnected") == 1


async def test_direct_connect_cancellation_after_commit_preserves_whole_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation after adoption must not leave C/E/R/watchdog half detached."""

    udid = "udid-direct-cancel"
    ip = "192.0.2.64"
    port = 49152
    dm = _LeaseDeviceManager(udid)
    _patch_device_manager(monkeypatch, dm)
    runner = _Runner("R-direct-cancel")
    engine = _Engine("E1")

    async def create_engine(_udid: str) -> None:
        state.simulation_engines[udid] = engine

    state = _patch_state(monkeypatch, create_engine)
    monkeypatch.setattr(device, "TunnelRunner", lambda: runner)
    monkeypatch.setattr(
        device,
        "_build_tunnel_udid_candidates",
        lambda _req: [udid],
    )

    observed: list[tuple[tuple, dict]] = []
    rearmed = asyncio.Event()
    original_watchdog = tunnel_manager._per_tunnel_watchdog

    async def observe_watchdog(*args, **kwargs) -> None:
        observed.append((args, kwargs))
        if kwargs.get("connection_lease") is not None:
            rearmed.set()
        await original_watchdog(*args, **kwargs)

    monkeypatch.setattr(device, "_per_tunnel_watchdog", observe_watchdog)
    monkeypatch.setattr(tunnel_manager, "_TUNNEL_RESTART_BACKOFF", ())

    async def no_fallback(_ip: str) -> list[tuple[str, int]]:
        return []

    monkeypatch.setattr(tunnel_manager, "find_fallback_endpoints", no_fallback)
    connected_started = asyncio.Event()
    release_connected = asyncio.Event()
    events: list[tuple[str, dict]] = []

    async def blocked_broadcast(event_type: str, data: dict) -> None:
        if event_type == "device_connected":
            connected_started.set()
            await release_connected.wait()
        events.append((event_type, data))

    monkeypatch.setattr("api.websocket.broadcast", blocked_broadcast)

    started = await device.wifi_tunnel_start(
        device.WifiTunnelStartRequest(
            ip=ip,
            port=port,
            udid=udid,
        ),
    )
    connect = asyncio.create_task(
        device.wifi_tunnel_connect(
            device.WifiTunnelConnectRequest(
                rsd_address=started["rsd_address"],
                rsd_port=started["rsd_port"],
            ),
        ),
    )
    try:
        await asyncio.wait_for(rearmed.wait(), timeout=0.5)
        await asyncio.wait_for(connected_started.wait(), timeout=0.5)
        connect.cancel()
        release_connected.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(connect, timeout=0.5)

        # A committed direct-connect lifecycle is indivisible from the
        # caller's perspective.  Implementations may choose full rollback,
        # but must never leave only some of C1/E1/R1/watchdog installed.
        current_connection = dm._connections.get(udid)
        current_engine = state.simulation_engines.get(udid)
        current_runner = tunnel_manager._tunnels.get(udid)
        current_watchdog = tunnel_manager._tunnel_watchdogs.get(udid)
        retained = any(
            item is not None
            for item in (
                current_connection,
                current_engine,
                current_runner,
                current_watchdog,
            )
        )
        if retained:
            assert current_connection is not None
            assert current_connection.name == "C1"
            assert current_engine is engine
            assert current_runner is runner
            assert current_watchdog is not None
            assert not current_watchdog.done()
            assert any(
                kwargs.get("connection_lease") is current_connection
                and kwargs.get("connection_engine") is current_engine
                for _args, kwargs in observed
            )
        else:
            assert dm._connections == {}
            assert state.simulation_engines == {}
            assert runner.task is not None and runner.task.done()
    finally:
        release_connected.set()
        if not connect.done():
            connect.cancel()
        await asyncio.gather(connect, return_exceptions=True)


class _FailThenSuccessRunner(_Runner):
    first_started: asyncio.Event | None = None
    release_first: asyncio.Event | None = None
    created = 0

    def __init__(self, name: str) -> None:
        super().__init__(name)
        type(self).created += 1

    async def start(self, udid: str, ip: str, port: int, timeout: float = 10.0) -> dict:
        if self.name == "R1":
            assert type(self).first_started is not None
            assert type(self).release_first is not None
            type(self).first_started.set()
            await type(self).release_first.wait()
            raise RuntimeError("first direct start failed")
        return await super().start(udid, ip, port, timeout)


async def test_concurrent_same_udid_failed_direct_start_cannot_stop_successor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed A request must not clean up B's later same-UDID runner."""

    udid = "udid-concurrent-direct"
    dm = _LeaseDeviceManager(udid)
    _patch_device_manager(monkeypatch, dm)
    monkeypatch.setattr(
        device,
        "_build_tunnel_udid_candidates",
        lambda _req: [udid],
    )
    monkeypatch.setattr(device, "_scan_ports_for_ip", lambda _ip: _empty_scan())

    first_started = asyncio.Event()
    release_first = asyncio.Event()
    _FailThenSuccessRunner.first_started = first_started
    _FailThenSuccessRunner.release_first = release_first
    _FailThenSuccessRunner.created = 0
    created: list[_FailThenSuccessRunner] = []
    names = iter(("R1", "R2"))

    def make_runner() -> _FailThenSuccessRunner:
        runner = _FailThenSuccessRunner(next(names))
        created.append(runner)
        return runner

    monkeypatch.setattr(device, "TunnelRunner", make_runner)

    async def idle_watchdog(*_args, **_kwargs) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(device, "_per_tunnel_watchdog", idle_watchdog)

    request = device.WifiTunnelStartRequest(
        ip="192.0.2.62",
        port=49152,
        udid=udid,
    )
    first = asyncio.create_task(device.wifi_tunnel_start(request))
    second: asyncio.Task | None = None
    try:
        await asyncio.wait_for(first_started.wait(), timeout=0.5)
        second = asyncio.create_task(device.wifi_tunnel_start(request))
        await asyncio.sleep(0)
        assert not second.done()
        assert [runner.name for runner in created] == ["R1"], (
            "R2 must not be constructed before A releases the lane"
        )

        release_first.set()
        with pytest.raises(HTTPException) as first_error:
            await asyncio.wait_for(first, timeout=0.5)
        assert first_error.value.status_code == 500
        assert first_error.value.detail["code"] == "tunnel_spawn_failed"

        result = await asyncio.wait_for(second, timeout=0.5)
        assert result["status"] == "started"
        assert result["udid"] == udid
        successor = tunnel_manager._tunnels[udid]
        assert getattr(successor, "name", None) == "R2"
        assert _FailThenSuccessRunner.created == 2
        assert created[0].stop_calls >= 1
        assert created[1].stop_calls == 0
    finally:
        release_first.set()
        for task in (first, second):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (first, second) if task is not None),
            return_exceptions=True,
        )


async def _empty_scan(_ip: str) -> list[int]:
    return []


def _assert_replacement_order(trace: list[str], success_marker: str) -> None:
    """Assert the externally visible C/E replacement transaction order."""

    assert trace.index("E0.stop_completed") < trace.index("C0.close")
    assert trace.index("C0.close") < trace.index("E1.create")
    assert trace.index("E1.create") < trace.index(success_marker)


async def test_direct_connect_stops_old_engine_before_closing_previous_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct WiFi replacement must drain E0 before DM closes C0."""

    udid = "udid-order-direct"
    trace: list[str] = []
    dm = _OrderedReplacementDeviceManager(udid, trace)
    old_connection = _Connection("C0")
    old_connection.connection_type = "USB"
    dm._connections[udid] = old_connection
    _patch_device_manager(monkeypatch, dm)
    _patch_broadcast(monkeypatch)

    old_engine = _TraceEngine("E0", trace)
    new_engine = _TraceEngine("E1", trace)

    async def create_engine(_udid: str) -> None:
        trace.append("E1.create")
        state.simulation_engines[udid] = new_engine

    state = _patch_state(monkeypatch, create_engine)
    state.simulation_engines[udid] = old_engine
    state._primary_udid = udid

    result = await device.wifi_tunnel_connect(
        device.WifiTunnelConnectRequest(
            rsd_address="fd00::order-direct",
            rsd_port=49152,
        ),
    )
    assert result["status"] == "connected"
    trace.append("direct.success")

    _assert_replacement_order(trace, "direct.success")


async def test_composite_start_and_connect_stops_old_engine_before_closing_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Composite start/connect must use the same C/E replacement order."""

    udid = "udid-order-composite"
    temp_key = "pending:192.0.2.72:49152"
    trace: list[str] = []
    dm = _OrderedReplacementDeviceManager(udid, trace)
    old_connection = _Connection("C0")
    old_connection.connection_type = "USB"
    dm._connections[udid] = old_connection
    _patch_device_manager(monkeypatch, dm)
    _patch_broadcast(monkeypatch)

    old_engine = _TraceEngine("E0", trace)
    new_engine = _TraceEngine("E1", trace)

    async def create_engine(_udid: str) -> None:
        trace.append("E1.create")
        state.simulation_engines[udid] = new_engine

    state = _patch_state(monkeypatch, create_engine)
    state.simulation_engines[udid] = old_engine
    state._primary_udid = udid
    monkeypatch.setattr(
        device,
        "_build_tunnel_udid_candidates",
        lambda _req: [temp_key],
    )
    monkeypatch.setattr(device, "TunnelRunner", lambda: _Runner("R-order"))

    async def idle_watchdog(*_args, **_kwargs) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(device, "_per_tunnel_watchdog", idle_watchdog)

    result = await device.wifi_tunnel_start_and_connect(
        device.WifiTunnelStartRequest(ip="192.0.2.72", port=49152),
    )
    assert result["status"] == "connected"
    trace.append("composite.success")

    _assert_replacement_order(trace, "composite.success")


async def test_watchdog_restart_stops_old_engine_before_closing_previous_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Watchdog restart must quiesce E0 before replacing C0 with C1."""

    udid = "udid-order-restart"
    trace: list[str] = []
    dm = _OrderedReplacementDeviceManager(udid, trace)
    old_connection = _Connection("C0")
    dm._connections[udid] = old_connection
    _patch_device_manager(monkeypatch, dm)
    _patch_broadcast(monkeypatch)

    old_engine = _TraceEngine("E0", trace)
    new_engine = _TraceEngine("E1", trace)

    async def create_engine(_udid: str) -> None:
        trace.append("E1.create")
        state.simulation_engines[udid] = new_engine

    state = _patch_state(monkeypatch, create_engine)
    state.simulation_engines[udid] = old_engine
    state._primary_udid = udid

    original_runner = _Runner("R0")
    replacement_runner = _Runner("R1")
    monkeypatch.setattr(tunnel_manager, "TunnelRunner", lambda: replacement_runner)

    async def idle_watchdog(*_args, **_kwargs) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(tunnel_manager, "_per_tunnel_watchdog", idle_watchdog)
    tunnel_manager._tunnels[udid] = original_runner
    tunnel_manager._tunnel_generations[udid] = 1

    result = await tunnel_manager._attempt_tunnel_restart(
        udid,
        "192.0.2.72",
        49152,
        {"kind": "navigate"},
        original_runner,
        connection_lease=old_connection,
    )
    assert result is True
    trace.append("restart.success")

    _assert_replacement_order(trace, "restart.success")

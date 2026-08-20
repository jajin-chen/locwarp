"""Deterministic ownership tests for watchdog restart generations.

The watchdog temporarily swaps a replacement runner into the registry while
post-setup side effects run.  A failed setup must restore the original runner
and its generation, but a stop that linearizes first must make that rollback
ineligible.  Side-effect tasks spawned during the same window must also be
owned by the stopped generation and be cancelled before the old watchdog can
return.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import services.tunnel_manager as tunnel_manager


class _Runner:
    instances: list["_Runner"] = []

    def __init__(self, name: str) -> None:
        self.name = name
        self.info: dict | None = None
        self.target_ip: str | None = None
        self.target_port: int | None = None
        self.stop_calls = 0
        self.instances.append(self)

    async def start(self, _udid: str, ip: str, port: int, timeout: float = 10.0) -> dict:
        self.target_ip = ip
        self.target_port = port
        self.info = {
            "rsd_address": f"fd00::{self.name}",
            "rsd_port": 12345,
            "interface": "fake",
        }
        return dict(self.info)

    async def stop(self) -> None:
        self.stop_calls += 1


class _DeviceManager:
    def __init__(
        self,
        device_info: SimpleNamespace,
        *,
        entered: asyncio.Event | None = None,
        release: asyncio.Event | None = None,
        network_connection: bool = False,
    ) -> None:
        self._connections: dict[str, object] = {}
        self.device_info = device_info
        self.entered = entered
        self.release = release
        self.network_connection = network_connection
        self.disconnect_calls: list[str] = []
        self.close_calls: list[tuple[str, object]] = []

    async def connect_wifi_tunnel_owned(
        self,
        address: str,
        port: int,
        before_close_previous=None,
        **kwargs,
    ) -> tuple[SimpleNamespace, object | None]:
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            await self.release.wait()
        callback = before_close_previous
        if callback is None:
            callback = next(
                (value for value in kwargs.values() if callable(value)),
                None,
            )
        previous = self._connections.get(self.device_info.udid)
        if callback is not None:
            result = callback(self.device_info.udid, previous)
            if asyncio.iscoroutine(result):
                await result
        lease = None
        if self.network_connection:
            lease = SimpleNamespace(
                connection_type="Network",
            )
            self._connections[self.device_info.udid] = lease
        return self.device_info, lease

    async def connect_wifi_tunnel(self, address: str, port: int) -> SimpleNamespace:
        info, _lease = await self.connect_wifi_tunnel_owned(address, port)
        return info

    async def _detach_connection(
        self,
        udid: str,
        *,
        expected: object | None = None,
    ) -> object | None:
        current = self._connections.get(udid)
        if expected is not None and current is not expected:
            return None
        return self._connections.pop(udid, None)

    async def _close_detached_connection(self, udid: str, conn: object) -> None:
        self.close_calls.append((udid, conn))

    async def disconnect(
        self,
        udid: str,
        *,
        expected: object | None = None,
    ) -> None:
        conn = await self._detach_connection(udid, expected=expected)
        if conn is not None:
            self.disconnect_calls.append(udid)
            await self._close_detached_connection(udid, conn)

    async def disconnect_if_current(self, udid: str, expected: object) -> bool:
        conn = await self._detach_connection(udid, expected=expected)
        if conn is None:
            return False
        self.disconnect_calls.append(udid)
        await self._close_detached_connection(udid, conn)
        return True


class _ExitedRunner:
    """Runner whose child has exited and has no retry endpoint."""

    def __init__(self) -> None:
        self.task = asyncio.create_task(asyncio.sleep(0))
        self.target_ip = None
        self.target_port = None
        self.stop_calls = 0

    def is_running(self) -> bool:
        return False

    async def stop(self) -> None:
        self.stop_calls += 1
        await asyncio.gather(self.task, return_exceptions=True)


class _TwoPhaseEngine:
    """Final cleanup emit blocks until a replacement wins the CAS."""

    def __init__(self) -> None:
        self.state = None
        self._stop_event = asyncio.Event()
        self._pause_event = asyncio.Event()
        self._active_task = None
        self.emit_calls = 0
        self.final_cleanup_started = asyncio.Event()
        self.release_final_cleanup = asyncio.Event()

    async def _emit(self, *_args) -> None:
        self.emit_calls += 1
        if self.emit_calls >= 1:
            self.final_cleanup_started.set()
            await self.release_final_cleanup.wait()


class _OwnedEngine:
    """Minimal engine that records the deterministic rollback stop."""

    def __init__(self) -> None:
        self.state = None
        self._stop_event = asyncio.Event()
        self._pause_event = asyncio.Event()
        self._active_task = None
        self.emit_calls = 0

    async def _emit(self, *_args) -> None:
        self.emit_calls += 1


class _BlockingChild:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.release = asyncio.Event()
        self.task: asyncio.Task | None = None
        self.late_side_effect = False

    async def run(self) -> None:
        self.task = asyncio.current_task()
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        self.late_side_effect = True


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
        tunnel_manager._pending_tunnel_starts.clear()
        tunnel_manager._tunnel_stop_watermarks.clear()
        tunnel_manager._tunnel_generations.clear()
        tunnel_manager._tunnel_side_effects.clear()
        tunnel_manager._tunnel_start_sequence = 0
        tunnel_manager._tunnel_stop_all_watermark = 0
        _Runner.instances.clear()

    await drain()
    yield
    await drain()


def _patch_app_state(monkeypatch: pytest.MonkeyPatch, *, create_engine) -> object:
    import main

    state = main.app_state
    monkeypatch.setattr(state, "simulation_engines", {})
    monkeypatch.setattr(state, "_primary_udid", None)
    monkeypatch.setattr(state, "create_engine_for_device", create_engine)
    return state


def _seed_original(udid: str, generation: int) -> _Runner:
    original = _Runner("original")
    tunnel_manager._tunnels[udid] = original  # type: ignore[assignment]
    tunnel_manager._tunnel_generations[udid] = generation
    assert tunnel_manager._tunnel_owner_locked(udid, original, generation)
    return original


async def test_restart_post_setup_failure_restores_original_runner_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    udid = "udid-1"
    original_generation = 7
    original = _seed_original(udid, original_generation)
    replacement = _Runner("replacement")
    monkeypatch.setattr(tunnel_manager, "TunnelRunner", lambda: replacement)

    info = SimpleNamespace(udid=udid, name="Phone", ios_version="17.5")
    dm = _DeviceManager(info, network_connection=True)
    monkeypatch.setattr(
        tunnel_manager,
        "_dm",
        lambda: dm,
    )

    async def fail_create_engine(_udid: str) -> None:
        raise RuntimeError("post-setup failed")

    _patch_app_state(monkeypatch, create_engine=fail_create_engine)

    result = await tunnel_manager._attempt_tunnel_restart(
        udid,
        "192.0.2.10",
        51234,
        {"kind": "navigate"},
        original,
    )

    assert result is False
    assert tunnel_manager._tunnels[udid] is original
    assert tunnel_manager._tunnel_generations[udid] == original_generation
    assert tunnel_manager._tunnel_owner_locked(
        udid, original, original_generation,
    )
    assert replacement.stop_calls == 1
    assert [item[0] for item in dm.close_calls] == [udid]
    assert udid not in dm._connections


async def test_restart_rollback_stops_only_engine_created_by_owned_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rollback of an owned restart must drain its Enew, not the old owner."""

    udid = "udid-1"
    original_generation = 7
    original = _seed_original(udid, original_generation)
    replacement = _Runner("replacement")
    monkeypatch.setattr(tunnel_manager, "TunnelRunner", lambda: replacement)

    info = SimpleNamespace(udid=udid, name="Phone", ios_version="17.5")
    dm = _DeviceManager(info, network_connection=True)
    monkeypatch.setattr(tunnel_manager, "_dm", lambda: dm)
    new_engine = _OwnedEngine()

    async def fail_after_engine_creation(_udid: str) -> None:
        import main

        main.app_state.simulation_engines[udid] = new_engine
        raise RuntimeError("post-setup failed after Enew")

    state = _patch_app_state(monkeypatch, create_engine=fail_after_engine_creation)

    result = await tunnel_manager._attempt_tunnel_restart(
        udid,
        "192.0.2.10",
        51234,
        {"kind": "navigate"},
        original,
    )

    assert result is False
    assert replacement.stop_calls == 1
    assert original.stop_calls == 0
    assert new_engine._stop_event.is_set(), (
        f"state_engines={state.simulation_engines!r} "
        f"emit_calls={new_engine.emit_calls} close_calls={dm.close_calls!r} "
        f"connections={dm._connections!r}"
    )
    assert udid not in state.simulation_engines
    assert dm.close_calls and dm.close_calls[0][0] == udid
    assert tunnel_manager._tunnels[udid] is original
    assert tunnel_manager._tunnel_generations[udid] == original_generation


async def test_restart_rollback_does_not_resurrect_after_stop_detaches_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    udid = "udid-1"
    original_generation = 7
    original = _seed_original(udid, original_generation)
    replacement = _Runner("replacement")
    monkeypatch.setattr(tunnel_manager, "TunnelRunner", lambda: replacement)

    entered = asyncio.Event()
    release = asyncio.Event()
    info = SimpleNamespace(udid=udid, name="Phone", ios_version="17.5")
    dm = _DeviceManager(
        info,
        entered=entered,
        release=release,
        network_connection=True,
    )
    monkeypatch.setattr(
        tunnel_manager,
        "_dm",
        lambda: dm,
    )

    async def fail_create_engine(_udid: str) -> None:
        raise RuntimeError("post-setup failed after stop")

    _patch_app_state(monkeypatch, create_engine=fail_create_engine)

    newer: _Runner | None = None
    restart_task = asyncio.create_task(
        tunnel_manager._attempt_tunnel_restart(
            udid,
            "192.0.2.10",
            51234,
            {"kind": "navigate"},
            original,
        ),
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=0.5)
        detached, watchdog, side_effects = await tunnel_manager._detach_tunnel(
            udid,
            expected=replacement,
        )
        assert detached is replacement
        await tunnel_manager._stop_tunnel_parts(
            detached,
            watchdog,
            caller="test_stop_first",
            udid=udid,
            side_effects=side_effects,
        )

        # A newer owner can win immediately after the stop linearizes.  The
        # stale restart must not disconnect that owner's Network connection.
        newer = _Runner("newer")
        async with tunnel_manager._tunnels_lock:
            tunnel_manager._tunnels[udid] = newer
            tunnel_manager._tunnel_generations[udid] = original_generation + 2
        release.set()
        assert await asyncio.wait_for(restart_task, timeout=0.5) is False
    finally:
        release.set()
        if not restart_task.done():
            restart_task.cancel()
        await asyncio.gather(restart_task, return_exceptions=True)

    assert newer is not None
    assert tunnel_manager._tunnels[udid] is newer
    assert tunnel_manager._tunnel_generations[udid] > original_generation
    assert tunnel_manager._tunnels.get(udid) is not original
    assert dm.disconnect_calls == []
    # No C3 DM replacement won this scenario.  The restart-owned C2 lease
    # must therefore be detached and closed exactly once, while the newer
    # runner registry entry remains intact.
    assert len(dm.close_calls) == 1
    assert dm.close_calls[0][0] == udid
    assert replacement.stop_calls >= 1


async def test_restart_c2_connection_conflict_restores_runner_without_closing_c2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A C2 DM lease winning during setup must survive Rnew rollback."""

    udid = "udid-1"
    original_generation = 7
    original = _seed_original(udid, original_generation)
    replacement = _Runner("replacement")
    monkeypatch.setattr(tunnel_manager, "TunnelRunner", lambda: replacement)

    info = SimpleNamespace(udid=udid, name="Phone", ios_version="17.5")
    dm = _DeviceManager(info, network_connection=True)
    monkeypatch.setattr(tunnel_manager, "_dm", lambda: dm)
    new_engine = _OwnedEngine()
    c2 = SimpleNamespace(connection_type="Network", name="C2")

    async def fail_on_c2_conflict(_udid: str) -> None:
        import main

        main.app_state.simulation_engines[udid] = new_engine
        # The newer DM lease wins independently of the tunnel registry.  The
        # stale rollback must CAS-fail and leave this C2 untouched.
        dm._connections[udid] = c2
        raise RuntimeError("genuine C2 connection conflict")

    state = _patch_app_state(monkeypatch, create_engine=fail_on_c2_conflict)

    result = await tunnel_manager._attempt_tunnel_restart(
        udid,
        "192.0.2.10",
        51234,
        {"kind": "navigate"},
        original,
    )

    assert result is False
    assert tunnel_manager._tunnels[udid] is original
    assert tunnel_manager._tunnel_generations[udid] == original_generation
    assert replacement.stop_calls == 1
    assert original.stop_calls == 0
    assert dm._connections[udid] is c2
    assert dm.close_calls == []
    assert udid not in state.simulation_engines


async def test_restart_lost_ownership_preserves_c3_after_c2_cleanup_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stale Rnew cleanup must not close a C3/R2 replacement that won later."""

    udid = "udid-1"
    original_generation = 7
    original = _seed_original(udid, original_generation)
    replacement = _Runner("replacement")
    c2_runner = _Runner("c2")
    monkeypatch.setattr(tunnel_manager, "TunnelRunner", lambda: replacement)

    info = SimpleNamespace(udid=udid, name="Phone", ios_version="17.5")
    dm = _DeviceManager(info, network_connection=True)
    monkeypatch.setattr(tunnel_manager, "_dm", lambda: dm)
    c3 = SimpleNamespace(connection_type="Network", name="C3")
    e2 = _OwnedEngine()
    e3 = _OwnedEngine()

    async def fail_after_c3_replacement(_udid: str) -> None:
        import main

        # E2/C2 belong to this restart before a later lifecycle wins.
        main.app_state.simulation_engines[udid] = e2
        # C3/R2/E3 are the newer owner.  Rollback must use identity CAS and
        # leave every one of these replacement resources untouched.
        dm._connections[udid] = c3
        main.app_state.simulation_engines[udid] = e3
        async with tunnel_manager._tunnels_lock:
            tunnel_manager._tunnels[udid] = c2_runner
            tunnel_manager._tunnel_generations[udid] = original_generation + 2
        raise RuntimeError("C3 replaced restart-owned C2")

    state = _patch_app_state(monkeypatch, create_engine=fail_after_c3_replacement)

    result = await tunnel_manager._attempt_tunnel_restart(
        udid,
        "192.0.2.10",
        51234,
        {"kind": "navigate"},
        original,
    )

    assert result is False
    assert replacement.stop_calls == 1
    assert original.stop_calls == 0
    assert tunnel_manager._tunnels[udid] is c2_runner
    assert tunnel_manager._tunnel_generations[udid] == original_generation + 2
    assert c2_runner.stop_calls == 0
    assert dm._connections[udid] is c3
    assert dm.close_calls == []
    assert state.simulation_engines[udid] is e3


async def test_restart_cancel_during_engine_creation_rolls_back_every_new_resource(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation after Rnew/Gnew/Cnew/Enew install restores the old owner."""

    udid = "udid-1"
    original_generation = 7
    original = _seed_original(udid, original_generation)
    replacement = _Runner("replacement")
    monkeypatch.setattr(tunnel_manager, "TunnelRunner", lambda: replacement)

    info = SimpleNamespace(udid=udid, name="Phone", ios_version="17.5")
    dm = _DeviceManager(info, network_connection=True)
    monkeypatch.setattr(tunnel_manager, "_dm", lambda: dm)
    new_engine = _OwnedEngine()
    engine_installed = asyncio.Event()
    release_engine = asyncio.Event()

    async def block_after_engine_install(_udid: str) -> None:
        import main

        main.app_state.simulation_engines[udid] = new_engine
        engine_installed.set()
        await release_engine.wait()

    state = _patch_app_state(monkeypatch, create_engine=block_after_engine_install)
    restart = asyncio.create_task(
        tunnel_manager._attempt_tunnel_restart(
            udid,
            "192.0.2.10",
            51234,
            {"kind": "navigate"},
            original,
        ),
    )
    try:
        await asyncio.wait_for(engine_installed.wait(), timeout=0.5)
        assert tunnel_manager._tunnels[udid] is replacement
        assert tunnel_manager._tunnel_generations[udid] == original_generation + 1
        assert udid in dm._connections
        assert state.simulation_engines[udid] is new_engine

        restart.cancel()
        release_engine.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(restart, timeout=0.5)
    finally:
        release_engine.set()
        if not restart.done():
            restart.cancel()
        await asyncio.gather(restart, return_exceptions=True)

    assert tunnel_manager._tunnels[udid] is original
    assert tunnel_manager._tunnel_generations[udid] == original_generation
    assert replacement.stop_calls == 1
    assert original.stop_calls == 0
    assert udid not in dm._connections
    assert [item[0] for item in dm.close_calls] == [udid]
    assert new_engine._stop_event.is_set()
    assert udid not in state.simulation_engines


async def test_restart_lost_registry_cleans_new_resources_without_resurrecting_original(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If stop already detached Rnew, rollback must not resurrect Roriginal."""

    udid = "udid-1"
    original_generation = 7
    original = _seed_original(udid, original_generation)
    replacement = _Runner("replacement")
    monkeypatch.setattr(tunnel_manager, "TunnelRunner", lambda: replacement)

    info = SimpleNamespace(udid=udid, name="Phone", ios_version="17.5")
    dm = _DeviceManager(info, network_connection=True)
    monkeypatch.setattr(tunnel_manager, "_dm", lambda: dm)
    new_engine = _OwnedEngine()

    async def fail_after_stop_wins(_udid: str) -> None:
        import main

        main.app_state.simulation_engines[udid] = new_engine
        detached, watchdog, side_effects = await tunnel_manager._detach_tunnel(
            udid,
            expected=replacement,
        )
        assert detached is replacement
        assert watchdog is None
        assert side_effects == ()
        raise RuntimeError("stop won before restart rollback")

    state = _patch_app_state(monkeypatch, create_engine=fail_after_stop_wins)

    result = await tunnel_manager._attempt_tunnel_restart(
        udid,
        "192.0.2.10",
        51234,
        {"kind": "navigate"},
        original,
    )

    assert result is False
    assert udid not in tunnel_manager._tunnels
    assert tunnel_manager._tunnel_generations[udid] > original_generation
    assert replacement.stop_calls == 1
    assert original.stop_calls == 0
    assert udid not in dm._connections
    assert [item[0] for item in dm.close_calls] == [udid]
    assert new_engine._stop_event.is_set()
    assert udid not in state.simulation_engines


@pytest.mark.parametrize(
    "snapshot_kind",
    [pytest.param("resume", id="resume"), pytest.param("auto-sync", id="auto-sync")],
)
async def test_restart_side_effect_child_is_cancelled_after_stop_detaches_old_watchdog(
    monkeypatch: pytest.MonkeyPatch,
    snapshot_kind: str,
) -> None:
    """A stopped generation must not leave resume/auto-sync work running."""

    udid = "udid-1"
    original = _seed_original(udid, 7)
    replacement = _Runner("replacement")
    monkeypatch.setattr(tunnel_manager, "TunnelRunner", lambda: replacement)
    child = _BlockingChild()

    info = SimpleNamespace(udid=udid, name="Phone", ios_version="17.5")
    monkeypatch.setattr(tunnel_manager, "_dm", lambda: _DeviceManager(info))

    async def create_engine(_udid: str) -> None:
        import main
        if snapshot_kind == "resume":
            main.app_state.simulation_engines[udid] = SimpleNamespace(
                resume_from_snapshot=lambda _snapshot: child.run(),
            )

    state = _patch_app_state(monkeypatch, create_engine=create_engine)
    if snapshot_kind == "auto-sync":
        async def auto_sync(_udid: str) -> None:
            await child.run()

        monkeypatch.setattr("main._auto_sync_new_device_to_primary", auto_sync)

    import api.websocket as websocket

    detached = False

    async def broadcast(_event_type: str, _data: dict) -> None:
        nonlocal detached
        if detached:
            return
        await asyncio.wait_for(child.started.wait(), timeout=0.5)
        runner, watchdog, side_effects = await tunnel_manager._detach_tunnel(
            udid,
            expected=replacement,
        )
        assert runner is replacement
        detached = True
        await tunnel_manager._stop_tunnel_parts(
            runner,
            watchdog,
            caller="test_stop_side_effect_child",
            udid=udid,
            side_effects=side_effects,
        )

    monkeypatch.setattr(websocket, "broadcast", broadcast)

    snapshot = {"kind": "navigate"} if snapshot_kind == "resume" else None
    result = await tunnel_manager._attempt_tunnel_restart(
        udid,
        "192.0.2.10",
        51234,
        snapshot,
        original,
    )

    try:
        assert result is True
        assert detached
        await asyncio.wait_for(child.cancelled.wait(), timeout=0.5)
        assert child.task is not None
        assert child.task.done()
        assert not child.late_side_effect
        assert state.simulation_engines is not None
    finally:
        child.release.set()
        if child.task is not None and not child.task.done():
            child.task.cancel()
        if child.task is not None:
            await asyncio.gather(child.task, return_exceptions=True)


async def test_watchdog_final_cleanup_cas_preserves_replacement_connection_and_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """G1 final cleanup must not close a C2/E2 replacement installed mid-emit."""

    udid = "udid-1"
    runner = _ExitedRunner()
    old_engine = _TwoPhaseEngine()
    new_engine = SimpleNamespace()
    info = SimpleNamespace(udid=udid, name="Phone", ios_version="17.5")
    dm = _DeviceManager(info, network_connection=True)
    dm._connections[udid] = SimpleNamespace(connection_type="Network", name="C1")
    monkeypatch.setattr(tunnel_manager, "_dm", lambda: dm)
    state = _patch_app_state(monkeypatch, create_engine=lambda _udid: asyncio.sleep(0))
    state.simulation_engines[udid] = old_engine
    state._primary_udid = udid

    events: list[str] = []

    async def broadcast(event_type: str, _data: dict) -> None:
        events.append(event_type)

    monkeypatch.setattr("api.websocket.broadcast", broadcast)
    tunnel_manager._tunnels[udid] = runner  # type: ignore[assignment]
    tunnel_manager._tunnel_generations[udid] = 1
    old_lease = dm._connections[udid]
    watchdog = asyncio.create_task(
        tunnel_manager._per_tunnel_watchdog(
            udid,
            runner,
            1,
            connection_lease=old_lease,
            connection_engine=old_engine,
        ),
    )
    tunnel_manager._tunnel_watchdogs[udid] = watchdog

    try:
        await asyncio.wait_for(old_engine.final_cleanup_started.wait(), timeout=0.5)
        replacement = SimpleNamespace(connection_type="Network", name="C2")
        dm._connections[udid] = replacement
        state.simulation_engines[udid] = new_engine
        replacement_runner = _ExitedRunner()
        tunnel_manager._tunnels[udid] = replacement_runner  # type: ignore[assignment]
        tunnel_manager._tunnel_generations[udid] = 2
        side_effect = _BlockingChild()
        await tunnel_manager._spawn_owned_tunnel_side_effect(
            udid,
            replacement_runner,
            2,
            side_effect.run,
        )
        await asyncio.wait_for(side_effect.started.wait(), timeout=0.5)

        old_engine.release_final_cleanup.set()
        await asyncio.wait_for(watchdog, timeout=0.5)

        # The finalizer owns C1 after its identity-CAS detaches it.  C2 is
        # installed only after that detach and therefore must remain live.
        assert [item[0] for item in dm.close_calls] == [udid]
        assert dm.disconnect_calls == []
        assert dm._connections[udid] is replacement
        assert state.simulation_engines[udid] is new_engine
        assert tunnel_manager._tunnels[udid] is replacement_runner
        assert "device_disconnected" not in events
        # The frontend has no generation token in this event contract.  Once
        # G2 owns the same UDID, G1 must not announce a stale tunnel_lost.
        assert events.count("tunnel_lost") == 0
        assert not side_effect.cancelled.is_set()
        assert side_effect.task is not None
        assert not side_effect.task.done()
        await tunnel_manager._cancel_tunnel_side_effects(udid, generation=2)
    finally:
        old_engine.release_final_cleanup.set()
        if not watchdog.done():
            watchdog.cancel()
        await asyncio.gather(watchdog, return_exceptions=True)
        for task in list(
            task
            for tasks in tunnel_manager._tunnel_side_effects.values()
            for task in tasks
        ):
            if not task.done():
                task.cancel()
        if tunnel_manager._tunnel_side_effects:
            await asyncio.gather(
                *[
                    task
                    for tasks in tunnel_manager._tunnel_side_effects.values()
                    for task in tasks
                ],
                return_exceptions=True,
            )


async def test_watchdog_final_cleanup_without_replacement_emits_tunnel_lost_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An owner that truly loses its tunnel emits exactly one loss event."""

    udid = "udid-1"
    runner = _ExitedRunner()
    old_engine = _TwoPhaseEngine()
    info = SimpleNamespace(udid=udid, name="Phone", ios_version="17.5")
    dm = _DeviceManager(info, network_connection=True)
    lease = SimpleNamespace(connection_type="Network", name="C1")
    dm._connections[udid] = lease
    monkeypatch.setattr(tunnel_manager, "_dm", lambda: dm)
    state = _patch_app_state(monkeypatch, create_engine=lambda _udid: asyncio.sleep(0))
    state.simulation_engines[udid] = old_engine
    state._primary_udid = udid
    events: list[str] = []

    async def broadcast(event_type: str, _data: dict) -> None:
        events.append(event_type)

    monkeypatch.setattr("api.websocket.broadcast", broadcast)
    tunnel_manager._tunnels[udid] = runner  # type: ignore[assignment]
    tunnel_manager._tunnel_generations[udid] = 1
    watchdog = asyncio.create_task(
        tunnel_manager._per_tunnel_watchdog(
            udid,
            runner,
            1,
            connection_lease=lease,
            connection_engine=old_engine,
        ),
    )
    tunnel_manager._tunnel_watchdogs[udid] = watchdog

    try:
        await asyncio.wait_for(old_engine.final_cleanup_started.wait(), timeout=0.5)
        old_engine.release_final_cleanup.set()
        await asyncio.wait_for(watchdog, timeout=0.5)

        assert dm._connections == {}
        assert state.simulation_engines == {}
        assert events.count("tunnel_lost") == 1
    finally:
        old_engine.release_final_cleanup.set()
        if not watchdog.done():
            watchdog.cancel()
        await asyncio.gather(watchdog, return_exceptions=True)


async def test_watchdog_final_cleanup_after_adoption_abort_emits_tunnel_lost_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A C0/E0 already removed by adoption abort still reports one loss."""

    udid = "udid-aborted"
    runner = _ExitedRunner()
    old_lease = SimpleNamespace(connection_type="Network", name="C0")
    old_engine = _TwoPhaseEngine()
    info = SimpleNamespace(udid=udid, name="Phone", ios_version="17.5")
    dm = _DeviceManager(info, network_connection=True)
    # Adoption abort has already exact-detached C0 and removed E0.  Keep the
    # stale identities in the watchdog's lease so final cleanup can still
    # decide whether its owner truly lost the tunnel.
    monkeypatch.setattr(tunnel_manager, "_dm", lambda: dm)
    state = _patch_app_state(monkeypatch, create_engine=lambda _udid: asyncio.sleep(0))
    state._primary_udid = udid
    events: list[tuple[str, dict]] = []

    async def broadcast(event_type: str, payload: dict) -> None:
        events.append((event_type, dict(payload)))

    monkeypatch.setattr("api.websocket.broadcast", broadcast)
    tunnel_manager._tunnels[udid] = runner  # type: ignore[assignment]
    tunnel_manager._tunnel_generations[udid] = 1
    watchdog = asyncio.create_task(
        tunnel_manager._per_tunnel_watchdog(
            udid,
            runner,
            1,
            connection_lease=old_lease,
            connection_engine=old_engine,
        ),
    )
    tunnel_manager._tunnel_watchdogs[udid] = watchdog

    try:
        await asyncio.wait_for(old_engine.final_cleanup_started.wait(), timeout=0.5)
        old_engine.release_final_cleanup.set()
        await asyncio.wait_for(watchdog, timeout=0.5)

        assert dm._connections == {}
        assert state.simulation_engines == {}
        lost = [
            (event_type, payload)
            for event_type, payload in events
            if event_type == "tunnel_lost"
        ]
        assert len(lost) == 1
        assert lost[0][1]["udid"] == udid
    finally:
        old_engine.release_final_cleanup.set()
        if not watchdog.done():
            watchdog.cancel()
        await asyncio.gather(watchdog, return_exceptions=True)


async def test_watchdog_final_cleanup_after_adoption_abort_suppresses_c2_e2_r2_loss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An adoption-aborted owner stays silent after C2/E2/R2 wins."""

    udid = "udid-aborted-replaced"
    runner = _ExitedRunner()
    old_lease = SimpleNamespace(connection_type="Network", name="C0")
    old_engine = _TwoPhaseEngine()
    info = SimpleNamespace(udid=udid, name="Phone", ios_version="17.5")
    dm = _DeviceManager(info, network_connection=True)
    monkeypatch.setattr(tunnel_manager, "_dm", lambda: dm)
    state = _patch_app_state(monkeypatch, create_engine=lambda _udid: asyncio.sleep(0))
    state._primary_udid = udid
    events: list[tuple[str, dict]] = []

    async def broadcast(event_type: str, payload: dict) -> None:
        events.append((event_type, dict(payload)))

    monkeypatch.setattr("api.websocket.broadcast", broadcast)
    tunnel_manager._tunnels[udid] = runner  # type: ignore[assignment]
    tunnel_manager._tunnel_generations[udid] = 1
    watchdog = asyncio.create_task(
        tunnel_manager._per_tunnel_watchdog(
            udid,
            runner,
            1,
            connection_lease=old_lease,
            connection_engine=old_engine,
        ),
    )
    tunnel_manager._tunnel_watchdogs[udid] = watchdog

    replacement = SimpleNamespace(connection_type="Network", name="C2")
    new_engine = SimpleNamespace(name="E2")
    replacement_runner = _ExitedRunner()
    try:
        await asyncio.wait_for(old_engine.final_cleanup_started.wait(), timeout=0.5)
        # C0/E0 were already removed by adoption abort.  A concurrent owner
        # now installs every identity that the finalizer must preserve.
        dm._connections[udid] = replacement
        state.simulation_engines[udid] = new_engine
        tunnel_manager._tunnels[udid] = replacement_runner  # type: ignore[assignment]
        tunnel_manager._tunnel_generations[udid] = 2
        old_engine.release_final_cleanup.set()
        await asyncio.wait_for(watchdog, timeout=0.5)

        assert dm._connections[udid] is replacement
        assert state.simulation_engines[udid] is new_engine
        assert tunnel_manager._tunnels[udid] is replacement_runner
        assert [
            event for event, _payload in events if event == "tunnel_lost"
        ] == []
    finally:
        old_engine.release_final_cleanup.set()
        if not watchdog.done():
            watchdog.cancel()
        await asyncio.gather(watchdog, return_exceptions=True)

"""Deterministic regressions for stopping a tunnel during its handshake.

The start route cannot publish a runner until the RemotePairing handshake has
returned usable RSD information.  A stop request arriving in that window must
still cancel (or mark) the in-flight start, and the eventual handshake result
must be disposed of rather than becoming an untracked tunnel.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from api import device
import services.tunnel_manager as tunnel_manager


class _FakeDeviceManager:
    _connections: dict[str, object] = {}

    async def discover_devices(self) -> list[object]:
        return []


class _HandshakeRunner:
    """Runner with a controllable handshake and an owned child task.

    ``TunnelRunner.start`` normally waits for a child task to publish RSD
    information while the child remains alive to carry the tunnel.  Keeping
    that shape here lets the regression prove both that the child is cleaned
    up and that its result is retrieved after a stop races the handshake.
    """

    instances: list["_HandshakeRunner"] = []

    def __init__(
        self,
        handshake_started: asyncio.Event,
        release_handshake: asyncio.Event,
    ) -> None:
        self._handshake_started = handshake_started
        self._release_handshake = release_handshake
        self._stop_requested = asyncio.Event()
        self._ready = asyncio.Event()
        self.child_task: asyncio.Task | None = None
        self.child_retrieved = False
        self.info: dict | None = None
        self.target_ip: str | None = None
        self.target_port: int | None = None
        self.stop_calls = 0
        self._running = False
        self.instances.append(self)

    def is_running(self) -> bool:
        return self._running

    async def _run_handshake_and_tunnel(self) -> None:
        self._handshake_started.set()
        try:
            await self._release_handshake.wait()
            self.info = {
                "rsd_address": "fd00::1",
                "rsd_port": 12345,
                "interface": "fake",
            }
            self._running = True
            self._ready.set()
            await self._stop_requested.wait()
        except asyncio.CancelledError:
            raise
        finally:
            self._running = False

    async def _finish_child(self, *, cancel: bool = False) -> None:
        task = self.child_task
        if task is None:
            return
        if cancel and not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if self.child_task is task:
            self.child_task = None
        self.child_retrieved = True

    async def start(self, _udid: str, ip: str, port: int, timeout: float = 20.0) -> dict:
        self.target_ip = ip
        self.target_port = port
        self.child_task = asyncio.create_task(self._run_handshake_and_tunnel())
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=timeout)
        except BaseException:
            self._stop_requested.set()
            await self._finish_child(cancel=True)
            raise
        return dict(self.info or {})

    async def stop(self) -> None:
        self.stop_calls += 1
        self._stop_requested.set()
        task = self.child_task
        # A stop arriving before the handshake must not wait for the network
        # handshake.  Once start has observed readiness, retrieve the child
        # before returning just like TunnelRunner.stop does.
        if task is not None and (self._ready.is_set() or task.done()):
            await self._finish_child()


@pytest.fixture(autouse=True)
async def clean_tunnel_registry() -> None:
    """Keep a failed race test from leaking a runner into its parametrization."""

    async def drain() -> None:
        for runner in _HandshakeRunner.instances:
            runner._release_handshake.set()
        watchdogs = list(device._tunnel_watchdogs.values())
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
        runners = list(device._tunnels.values())
        for runner in runners:
            stop = getattr(runner, "stop", None)
            if stop is not None:
                await stop()
        device._tunnel_watchdogs.clear()
        device._tunnels.clear()
        tunnel_manager._pending_tunnel_starts.clear()
        tunnel_manager._tunnel_stop_watermarks.clear()
        tunnel_manager._tunnel_generations.clear()
        tunnel_manager._tunnel_side_effects.clear()
        tunnel_manager._tunnel_start_sequence = 0
        tunnel_manager._tunnel_stop_all_watermark = 0
        for runner in _HandshakeRunner.instances:
            if runner.child_task is not None:
                await runner._finish_child(cancel=True)

    await drain()
    yield
    await drain()


@pytest.fixture
async def handshake_runner(monkeypatch: pytest.MonkeyPatch):
    handshake_started = asyncio.Event()
    release_handshake = asyncio.Event()
    _HandshakeRunner.instances = []

    def make_runner() -> _HandshakeRunner:
        return _HandshakeRunner(handshake_started, release_handshake)

    monkeypatch.setattr(device, "TunnelRunner", make_runner)
    monkeypatch.setattr(
        device,
        "_build_tunnel_udid_candidates",
        lambda _req: ["udid-1"],
    )
    monkeypatch.setattr(device, "_dm", lambda: _FakeDeviceManager())

    async def no_network_cleanup(_udid: str, *, caller: str) -> bool:
        return False

    monkeypatch.setattr(device, "_cleanup_wifi_connection_for", no_network_cleanup)

    async def idle_watchdog(*_args) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(device, "_per_tunnel_watchdog", idle_watchdog)
    return handshake_started, release_handshake


@pytest.mark.parametrize(
    "stop_request",
    [
        pytest.param(device.WifiTunnelStopRequest(udid="udid-1"), id="per-udid"),
        pytest.param(None, id="stop-all"),
    ],
)
async def test_stop_racing_handshake_marks_inflight_and_never_commits(
    handshake_runner,
    stop_request: device.WifiTunnelStopRequest | None,
) -> None:
    """A stop must win even though the runner is not in the registry yet."""

    handshake_started, release_handshake = handshake_runner
    start_task = asyncio.create_task(
        device.wifi_tunnel_start(
            device.WifiTunnelStartRequest(
                ip="192.0.2.10",
                port=49152,
                udid="udid-1",
            ),
        ),
    )
    try:
        await asyncio.wait_for(handshake_started.wait(), timeout=0.2)
        assert device._tunnels == {}
        assert device._tunnel_watchdogs == {}

        # This is deliberately before releasing the handshake.  A stop route
        # waiting on the start lock, or on the network handshake, is a deadlock
        # from the user's perspective.
        stop_result = await asyncio.wait_for(
            device.wifi_tunnel_stop(stop_request),
            timeout=0.2,
        )
        assert stop_result["status"] == "stopped"

        # Let the child finish its handshake.  The start request may return a
        # cancellation/HTTP error or a non-started status, but it must never
        # publish the runner/watchdog after the stop intent was recorded.
        release_handshake.set()
        with pytest.raises(HTTPException) as caught:
            await asyncio.wait_for(start_task, timeout=0.5)
        assert caught.value.status_code == 409
        assert caught.value.detail["code"] == "tunnel_start_cancelled"
    finally:
        release_handshake.set()
        if not start_task.done():
            start_task.cancel()
        await asyncio.gather(start_task, return_exceptions=True)

    assert device._tunnels == {}
    assert device._tunnel_watchdogs == {}
    assert _HandshakeRunner.instances
    runner = _HandshakeRunner.instances[0]
    assert runner.child_task is None
    assert runner.child_retrieved
    assert runner.stop_calls >= 1


async def test_second_start_budget_timeout_does_not_spawn_runner_while_first_holds_lock(
    handshake_runner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A queued start must time out at its deadline, before spawning a runner."""

    handshake_started, release_handshake = handshake_runner
    monkeypatch.setattr(device, "TUNNEL_START_BUDGET", 30.0)
    first_task = asyncio.create_task(
        device.wifi_tunnel_start(
            device.WifiTunnelStartRequest(
                ip="192.0.2.10",
                port=49152,
                udid="udid-1",
            ),
        ),
    )
    try:
        await asyncio.wait_for(handshake_started.wait(), timeout=0.2)
        assert len(_HandshakeRunner.instances) == 1
        assert device._tunnels == {}

        # The first request owns the serialized start lock.  A zero budget
        # gives the second request an already-expired end-to-end deadline, so
        # its lock acquisition must map immediately without real sleeping.
        monkeypatch.setattr(device, "TUNNEL_START_BUDGET", 0.0)
        with pytest.raises(HTTPException) as caught:
            await device.wifi_tunnel_start(
                device.WifiTunnelStartRequest(
                    ip="192.0.2.11",
                    port=49153,
                    udid="udid-1",
                ),
            )
        assert caught.value.status_code == 500
        assert caught.value.detail["code"] == "tunnel_timeout"
        assert len(_HandshakeRunner.instances) == 1
    finally:
        release_handshake.set()
        if not first_task.done():
            first_task.cancel()
        await asyncio.gather(first_task, return_exceptions=True)

    assert device._tunnels == {}
    assert device._tunnel_watchdogs == {}
    assert tunnel_manager._pending_tunnel_starts == {}
    assert _HandshakeRunner.instances[0].child_task is None


async def test_disconnect_device_fences_pending_handshake_and_cleans_child(
    handshake_runner,
) -> None:
    """Disconnect must cancel a matching start before it enters the registry."""

    handshake_started, release_handshake = handshake_runner
    start_task = asyncio.create_task(
        device.wifi_tunnel_start(
            device.WifiTunnelStartRequest(
                ip="192.0.2.10",
                port=49152,
                udid="udid-1",
            ),
        ),
    )
    try:
        await asyncio.wait_for(handshake_started.wait(), timeout=0.2)
        assert device._tunnels == {}
        assert device._tunnel_watchdogs == {}

        result = await asyncio.wait_for(
            device.disconnect_device("udid-1"),
            timeout=0.2,
        )
        assert result == {"status": "disconnected", "udid": "udid-1"}

        with pytest.raises(HTTPException) as caught:
            await asyncio.wait_for(start_task, timeout=0.5)
        assert caught.value.status_code == 409
        assert caught.value.detail["code"] == "tunnel_start_cancelled"
    finally:
        release_handshake.set()
        if not start_task.done():
            start_task.cancel()
        await asyncio.gather(start_task, return_exceptions=True)

    assert device._tunnels == {}
    assert device._tunnel_watchdogs == {}
    assert tunnel_manager._pending_tunnel_starts == {}
    assert tunnel_manager._tunnel_side_effects == {}
    runner = _HandshakeRunner.instances[0]
    assert runner.child_task is None
    assert runner.child_retrieved
    assert runner.stop_calls >= 1

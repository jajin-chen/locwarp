"""Focused regression coverage for RemotePairing port resolution.

The real iOS RemotePairing listener may move after a reboot or a WiFi
rebind.  These tests keep the port-resolution contract independent from a
real device: candidate ports are filtered before the handshake, a timeout
advances to the next port, and one fresh scan is allowed after the known
ports are exhausted.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from api import device
import services.tunnel_manager as tunnel_manager


@pytest.fixture(autouse=True)
async def clean_tunnel_state() -> None:
    """Do not leave fake runners or watchdog tasks in the shared registry."""

    async def drain() -> None:
        watchdogs = list(device._tunnel_watchdogs.values())
        for task in watchdogs:
            if not task.done():
                task.cancel()
        if watchdogs:
            await asyncio.gather(*watchdogs, return_exceptions=True)

        runners = list(device._tunnels.values())
        for runner in runners:
            stop = getattr(runner, "stop", None)
            if stop is not None:
                await stop()
        device._tunnel_watchdogs.clear()
        device._tunnels.clear()
        for task in (
            task
            for tasks in tunnel_manager._tunnel_side_effects.values()
            for task in tasks
        ):
            if not task.done():
                task.cancel()
        side_effects = [
            task
            for tasks in tunnel_manager._tunnel_side_effects.values()
            for task in tasks
        ]
        if side_effects:
            await asyncio.gather(*side_effects, return_exceptions=True)
        tunnel_manager._pending_tunnel_starts.clear()
        tunnel_manager._tunnel_stop_watermarks.clear()
        tunnel_manager._tunnel_generations.clear()
        tunnel_manager._tunnel_side_effects.clear()
        tunnel_manager._tunnel_start_sequence = 0
        tunnel_manager._tunnel_stop_all_watermark = 0

    await drain()
    yield
    await drain()


class FakeRunner:
    """Small TunnelRunner stand-in with per-port scripted outcomes."""

    outcomes: dict[object, BaseException | None] = {}
    calls: list[tuple[str, str, int, float]] = []
    instances: list["FakeRunner"] = []

    def __init__(self) -> None:
        self.info: dict | None = None
        self.udid: str | None = None
        self.target_ip: str | None = None
        self.target_port: int | None = None
        self._running = False
        self.stop_calls = 0
        self.stop_requested = asyncio.Event()
        self.instances.append(self)

    def is_running(self) -> bool:
        return self._running

    async def start(self, udid: str, ip: str, port: int, timeout: float = 20.0) -> dict:
        self.calls.append((udid, ip, port, timeout))
        self.udid = udid
        self.target_ip = ip
        self.target_port = port
        error = self.outcomes.get((udid, port), self.outcomes.get(port))
        if error is not None:
            raise error
        self.info = {
            "rsd_address": "fd00::1",
            "rsd_port": 12345,
            "interface": "fake",
        }
        self._running = True
        return dict(self.info)

    async def stop(self) -> None:
        self.stop_calls += 1
        self.stop_requested.set()
        self._running = False


class _ProbeDeviceManager:
    def __init__(self) -> None:
        self._connections: dict[str, object] = {}

    async def discover_devices(self) -> list[object]:
        return []

    async def disconnect(self, _udid: str, **_kwargs) -> None:
        return None


async def _idle_watchdog(*_args, **_kwargs) -> None:
    await asyncio.Event().wait()


@pytest.fixture
def fake_runner(monkeypatch: pytest.MonkeyPatch):
    FakeRunner.outcomes = {}
    FakeRunner.calls = []
    FakeRunner.instances = []
    monkeypatch.setattr(device, "TunnelRunner", FakeRunner)

    async def idle_watchdog(*_args) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(device, "_per_tunnel_watchdog", idle_watchdog)
    return FakeRunner


@pytest.fixture
def one_udid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(device, "_build_tunnel_udid_candidates", lambda _req: ["udid-1"])


@pytest.fixture
def two_udids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        device,
        "_build_tunnel_udid_candidates",
        lambda _req: ["udid-1", "udid-2"],
    )


def test_port_candidates_deduplicate_validate_and_exclude_lockdownd_port() -> None:
    req = device.WifiTunnelStartRequest(
        ip="192.0.2.10",
        port=49152,
        ports=[49152, 62078, 49153, 49153, 0, -1, 65536],
    )

    assert device._build_tunnel_port_candidates(req) == [49152, 49153]


async def test_timeout_on_first_port_advances_and_returns_actual_port(
    fake_runner,
    one_udid,
) -> None:
    fake_runner.outcomes = {49152: asyncio.TimeoutError()}

    result = await device.wifi_tunnel_start(
        device.WifiTunnelStartRequest(
            ip="192.0.2.10",
            port=49152,
            ports=[49153],
            udid="udid-1",
        ),
    )

    assert result["status"] == "started"
    assert result["port"] == 49153
    assert [call[2] for call in fake_runner.calls] == [49152, 49153]


async def test_timeout_for_first_udid_still_tries_second_udid_on_same_port(
    fake_runner,
    two_udids,
) -> None:
    fake_runner.outcomes = {
        ("udid-1", 49152): asyncio.TimeoutError(),
        ("udid-2", 49152): None,
    }

    result = await device.wifi_tunnel_start(
        device.WifiTunnelStartRequest(
            ip="192.0.2.10",
            port=49152,
            udid="udid-1",
        ),
    )

    assert result["status"] == "started"
    assert result["udid"] == "udid-2"
    assert result["port"] == 49152
    assert [(call[0], call[2]) for call in fake_runner.calls] == [
        ("udid-1", 49152),
        ("udid-2", 49152),
    ]


async def test_tiny_start_budget_is_hard_bound_and_maps_timeout_to_http_error(
    monkeypatch: pytest.MonkeyPatch,
    fake_runner,
    one_udid,
) -> None:
    """Use a deterministic loop clock instead of waiting for the budget."""

    class Clock:
        def time(self) -> float:
            # Keep lock/timeout bookkeeping inside the budget until the fake
            # runner has actually been attempted.  Once that first attempt
            # records its call, the next budget check observes expiry without
            # sleeping in real time or relying on a fixed number of loop.time
            # calls made by asyncio internals.
            return 100.2 if fake_runner.calls else 100.0

    clock = Clock()
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "time", clock.time)
    monkeypatch.setattr(device, "TUNNEL_START_BUDGET", 0.1)
    fake_runner.outcomes = {49152: asyncio.TimeoutError()}

    with pytest.raises(HTTPException) as caught:
        await device.wifi_tunnel_start(
            device.WifiTunnelStartRequest(
                ip="192.0.2.10",
                port=49152,
                ports=[49153],
                udid="udid-1",
            ),
        )

    assert caught.value.status_code == 500
    assert caught.value.detail["code"] == "tunnel_timeout"
    assert [call[2] for call in fake_runner.calls] == [49152]


async def test_known_ports_exhausted_scans_once_and_uses_fresh_port(
    monkeypatch: pytest.MonkeyPatch,
    fake_runner,
    one_udid,
) -> None:
    fake_runner.outcomes = {
        49152: RuntimeError("stale first port"),
        49153: RuntimeError("stale second port"),
        49154: None,
    }
    scans: list[str] = []

    async def scan(ip: str) -> list[int]:
        scans.append(ip)
        return [49152, 62078, 49154]

    monkeypatch.setattr(device, "_scan_ports_for_ip", scan)

    result = await device.wifi_tunnel_start(
        device.WifiTunnelStartRequest(
            ip="192.0.2.10",
            port=49152,
            ports=[49153],
            udid="udid-1",
        ),
    )

    assert result["status"] == "started"
    assert result["port"] == 49154
    assert scans == ["192.0.2.10"]
    assert [call[2] for call in fake_runner.calls] == [49152, 49153, 49154]


async def test_scan_without_new_port_preserves_spawn_failure_http_error(
    monkeypatch: pytest.MonkeyPatch,
    fake_runner,
    one_udid,
) -> None:
    fake_runner.outcomes = {
        49152: RuntimeError("stale first port"),
        49153: RuntimeError("stale second port"),
    }
    scan_count = 0

    async def scan(_ip: str) -> list[int]:
        nonlocal scan_count
        scan_count += 1
        return [49152, 62078, 49153]

    monkeypatch.setattr(device, "_scan_ports_for_ip", scan)

    with pytest.raises(HTTPException) as caught:
        await device.wifi_tunnel_start(
            device.WifiTunnelStartRequest(
                ip="192.0.2.10",
                port=49152,
                ports=[49153],
                udid="udid-1",
            ),
        )

    assert caught.value.status_code == 500
    assert caught.value.detail["code"] == "tunnel_spawn_failed"
    assert scan_count == 1
    assert [call[2] for call in fake_runner.calls] == [49152, 49153]


async def test_stop_of_active_candidate_probe_skips_only_that_udid_and_continues(
    fake_runner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A per-UDID fence must not abort the whole multi-candidate route.

    The first UDID has already timed out on the first port.  Its pair-record
    probe is then active while the stop request targets that UDID.  The route
    must dispose only that probe, continue with the next port, and allow the
    requested UDID to succeed there.
    """

    dm = _ProbeDeviceManager()
    monkeypatch.setattr(device, "_dm", lambda: dm)
    monkeypatch.setattr(tunnel_manager, "_dm", lambda: dm)
    monkeypatch.setattr(
        device,
        "_build_tunnel_udid_candidates",
        lambda _req: ["udid-2", "udid-1"],
    )

    probe_started = asyncio.Event()
    original_start = fake_runner.start

    async def controlled_start(self, udid: str, ip: str, port: int, timeout: float = 20.0):
        self.udid = udid
        self.target_ip = ip
        self.target_port = port
        if (udid, port) == ("udid-2", 49152):
            self.calls.append((udid, ip, port, timeout))
            raise asyncio.TimeoutError()
        if (udid, port) == ("udid-1", 49152):
            self.calls.append((udid, ip, port, timeout))
            probe_started.set()
            await self.stop_requested.wait()
            raise asyncio.TimeoutError()
        return await original_start(self, udid, ip, port, timeout)

    monkeypatch.setattr(fake_runner, "start", controlled_start)
    fake_runner.outcomes = {("udid-2", 49153): None}

    start_task = asyncio.create_task(
        device.wifi_tunnel_start(
            device.WifiTunnelStartRequest(
                ip="192.0.2.10",
                port=49152,
                ports=[49153],
                udid="udid-2",
            ),
        ),
    )
    try:
        await asyncio.wait_for(probe_started.wait(), timeout=0.5)
        stop_result = await asyncio.wait_for(
            device.wifi_tunnel_stop(device.WifiTunnelStopRequest(udid="udid-1")),
            timeout=0.5,
        )
        assert stop_result["status"] == "stopped"

        result = await asyncio.wait_for(start_task, timeout=0.5)
        assert result["status"] == "started"
        assert result["udid"] == "udid-2"
        assert result["port"] == 49153
        assert [(call[0], call[2]) for call in fake_runner.calls] == [
            ("udid-2", 49152),
            ("udid-1", 49152),
            ("udid-2", 49153),
        ]
        probe = next(
            runner
            for runner in fake_runner.instances
            if runner.udid == "udid-1" and runner.target_port == 49152
        )
        assert probe.stop_calls >= 1
        assert tunnel_manager._pending_tunnel_starts == {}
    finally:
        if not start_task.done():
            start_task.cancel()
        await asyncio.gather(start_task, return_exceptions=True)


async def test_external_start_cancellation_is_not_swallowed(
    fake_runner,
    one_udid,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller cancellation remains CancelledError, not a route retry."""

    probe_started = asyncio.Event()

    async def blocking_start(self, udid: str, ip: str, port: int, timeout: float = 20.0):
        self.target_ip = ip
        self.target_port = port
        probe_started.set()
        await asyncio.Event().wait()
        return {}

    # Keep the cancellation path independent of the stop-fence path: no stop
    # watermark is written for this request.  Assigning a plain function to
    # the class keeps ``self`` explicit and avoids an AsyncMock swallowing
    # CancelledError in this regression.
    monkeypatch.setattr(fake_runner, "start", blocking_start)
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
        await asyncio.wait_for(probe_started.wait(), timeout=0.5)
        start_task.cancel()
        # A repeated cancellation in the same cleanup window must not skip
        # runner retrieval or let the start finalizer leak its pending entry.
        start_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(start_task, timeout=0.5)
        assert fake_runner.instances[0].stop_calls >= 1
        assert tunnel_manager._pending_tunnel_starts == {}
    finally:
        if not start_task.done():
            start_task.cancel()
        await asyncio.gather(start_task, return_exceptions=True)


async def test_different_ip_probes_enter_their_lanes_concurrently(
    fake_runner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Different target IPs must not wait on one global probe lock."""

    ip_a = "192.0.2.70"
    ip_b = "192.0.2.71"
    started = {ip_a: asyncio.Event(), ip_b: asyncio.Event()}
    release = asyncio.Event()
    original_start = fake_runner.start

    monkeypatch.setattr(
        device,
        "_build_tunnel_udid_candidates",
        lambda req: [req.udid],
    )

    async def barrier_start(
        self,
        udid: str,
        ip: str,
        port: int,
        timeout: float = 20.0,
    ) -> dict:
        started[ip].set()
        await release.wait()
        return await original_start(self, udid, ip, port, timeout)

    monkeypatch.setattr(fake_runner, "start", barrier_start)
    task_a = asyncio.create_task(
        device.wifi_tunnel_start(
            device.WifiTunnelStartRequest(
                ip=ip_a,
                port=49152,
                udid="udid-a",
            ),
        ),
    )
    task_b = asyncio.create_task(
        device.wifi_tunnel_start(
            device.WifiTunnelStartRequest(
                ip=ip_b,
                port=49153,
                udid="udid-b",
            ),
        ),
    )
    try:
        await asyncio.wait_for(
            asyncio.gather(*(event.wait() for event in started.values())),
            timeout=0.5,
        )
        assert not task_a.done()
        assert not task_b.done()
    finally:
        release.set()
    results = await asyncio.gather(task_a, task_b)
    assert {result["udid"] for result in results} == {"udid-a", "udid-b"}
    assert {(call[0], call[1], call[2]) for call in fake_runner.calls} == {
        ("udid-a", ip_a, 49152),
        ("udid-b", ip_b, 49153),
    }


async def test_same_ip_different_ports_are_serialized_in_request_order(
    fake_runner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One IP lane keeps port probes ordered while allowing retries."""

    ip = "192.0.2.72"
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    original_start = fake_runner.start
    monkeypatch.setattr(device, "_build_tunnel_udid_candidates", lambda req: [req.udid])

    async def ordered_start(
        self,
        udid: str,
        target_ip: str,
        port: int,
        timeout: float = 20.0,
    ) -> dict:
        if port == 49152:
            self.calls.append((udid, target_ip, port, timeout))
            self.udid = udid
            self.target_ip = target_ip
            self.target_port = port
            first_started.set()
            await release_first.wait()
            raise RuntimeError("stale first endpoint")
        return await original_start(self, udid, target_ip, port, timeout)

    monkeypatch.setattr(fake_runner, "start", ordered_start)

    async def no_scan(_ip: str) -> list[int]:
        return []

    monkeypatch.setattr(device, "_scan_ports_for_ip", no_scan)
    first = asyncio.create_task(
        device.wifi_tunnel_start(
            device.WifiTunnelStartRequest(
                ip=ip,
                port=49152,
                udid="udid-same-ip",
            ),
        ),
    )
    second = asyncio.create_task(
        device.wifi_tunnel_start(
            device.WifiTunnelStartRequest(
                ip=ip,
                port=49153,
                udid="udid-same-ip",
            ),
        ),
    )
    try:
        await asyncio.wait_for(first_started.wait(), timeout=0.5)
        await asyncio.sleep(0)
        assert not second.done(), "same-IP port probe bypassed its lane"
        assert [call[2] for call in fake_runner.calls] == [49152]

        release_first.set()
        with pytest.raises(HTTPException) as first_error:
            await asyncio.wait_for(first, timeout=0.5)
        assert first_error.value.detail["code"] == "tunnel_spawn_failed"
        result = await asyncio.wait_for(second, timeout=0.5)
    finally:
        release_first.set()
        for task in (first, second):
            if not task.done():
                task.cancel()
        await asyncio.gather(first, second, return_exceptions=True)

    assert result["status"] == "started"
    assert result["port"] == 49153
    assert [call[2] for call in fake_runner.calls] == [49152, 49153]


async def test_start_lane_entry_is_released_after_timeout_and_cancellation(
    fake_runner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed or cancelled lane must accept the next request immediately."""

    ip = "192.0.2.73"
    original_start = fake_runner.start
    monkeypatch.setattr(device, "_build_tunnel_udid_candidates", lambda req: [req.udid])

    async def no_scan(_ip: str) -> list[int]:
        return []

    monkeypatch.setattr(device, "_scan_ports_for_ip", no_scan)
    fake_runner.outcomes = {49152: asyncio.TimeoutError()}
    with pytest.raises(HTTPException) as timed_out:
        await asyncio.wait_for(
            device.wifi_tunnel_start(
                device.WifiTunnelStartRequest(
                    ip=ip,
                    port=49152,
                    udid="udid-lane",
                ),
            ),
            timeout=0.5,
        )
    assert timed_out.value.detail["code"] == "tunnel_timeout"
    assert tunnel_manager._pending_tunnel_starts == {}

    probe_started = asyncio.Event()

    async def blocking_start(
        self,
        udid: str,
        target_ip: str,
        port: int,
        timeout: float = 20.0,
    ) -> dict:
        self.udid = udid
        self.target_ip = target_ip
        self.target_port = port
        probe_started.set()
        await asyncio.Event().wait()
        return {}

    monkeypatch.setattr(fake_runner, "start", blocking_start)
    cancelled = asyncio.create_task(
        device.wifi_tunnel_start(
            device.WifiTunnelStartRequest(
                ip=ip,
                port=49152,
                udid="udid-lane",
            ),
        ),
    )
    try:
        await asyncio.wait_for(probe_started.wait(), timeout=0.5)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(cancelled, timeout=0.5)
    finally:
        if not cancelled.done():
            cancelled.cancel()
        await asyncio.gather(cancelled, return_exceptions=True)
    assert tunnel_manager._pending_tunnel_starts == {}

    fake_runner.outcomes = {}
    monkeypatch.setattr(fake_runner, "start", original_start)
    result = await asyncio.wait_for(
        device.wifi_tunnel_start(
            device.WifiTunnelStartRequest(
                ip=ip,
                port=49152,
                udid="udid-lane",
            ),
        ),
        timeout=0.5,
    )
    assert result["status"] == "started"
    assert tunnel_manager._pending_tunnel_starts == {}


def test_start_lane_is_reusable_across_two_asyncio_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-IP lane state must not retain a lock bound to a closed loop."""

    monkeypatch.setattr(device, "_build_tunnel_udid_candidates", lambda req: [req.udid])
    monkeypatch.setattr(device, "_per_tunnel_watchdog", _idle_watchdog)

    async def no_scan(_ip: str) -> list[int]:
        return []

    monkeypatch.setattr(device, "_scan_ports_for_ip", no_scan)
    FakeRunner.outcomes = {}
    FakeRunner.calls = []
    FakeRunner.instances = []
    monkeypatch.setattr(device, "TunnelRunner", FakeRunner)

    async def invoke(*, fail: bool):
        FakeRunner.outcomes = {
            49152: RuntimeError("closed-loop lane probe failed"),
        } if fail else {}
        request = device.WifiTunnelStartRequest(
            ip="192.0.2.74",
            port=49152,
            udid="udid-loop",
        )
        try:
            return await device.wifi_tunnel_start(request)
        finally:
            runner, watchdog, side_effects = await tunnel_manager._detach_tunnel(
                "udid-loop",
            )
            await tunnel_manager._stop_tunnel_parts(
                runner,
                watchdog,
                side_effects=side_effects,
                caller="test_cross_loop_lane_cleanup",
                udid="udid-loop",
            )
            tunnel_manager._pending_tunnel_starts.clear()

    with pytest.raises(HTTPException) as failed:
        asyncio.run(invoke(fail=True))
    assert failed.value.detail["code"] == "tunnel_spawn_failed"
    result = asyncio.run(invoke(fail=False))
    assert result["status"] == "started"

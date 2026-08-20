"""Regression tests for bounded WiFi port scanning."""

from __future__ import annotations

import asyncio

import pytest

from services import tunnel_discovery


async def test_tcp_probe_applies_its_timeout_once(monkeypatch: pytest.MonkeyPatch) -> None:
    wait_for_calls: list[float] = []

    class FakeWriter:
        def close(self) -> None:
            pass

        async def wait_closed(self) -> None:
            pass

    async def open_connection(_ip: str, _port: int):
        return object(), FakeWriter()

    async def wait_for(awaitable, timeout: float):
        wait_for_calls.append(timeout)
        return await awaitable

    monkeypatch.setattr(tunnel_discovery.asyncio, "open_connection", open_connection)
    monkeypatch.setattr(tunnel_discovery.asyncio, "wait_for", wait_for)

    assert await tunnel_discovery._tcp_probe("192.0.2.10", 49152, timeout=0.35)
    assert wait_for_calls == [0.35]


async def test_port_scan_cancels_all_probe_tasks_when_scan_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = 3
    started = asyncio.Event()
    probe_tasks: list[asyncio.Task] = []
    cancelled_ports: list[int] = []

    async def blocking_probe(_ip: str, port: int, _timeout: float) -> bool:
        task = asyncio.current_task()
        assert task is not None
        probe_tasks.append(task)
        if len(probe_tasks) == expected:
            started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled_ports.append(port)
            raise

    monkeypatch.setattr(tunnel_discovery, "_tcp_probe", blocking_probe)
    scan_task = asyncio.create_task(
        tunnel_discovery._scan_ports_for_ip(
            "192.0.2.10",
            start=49152,
            end=49154,
            concurrency=expected,
            timeout=0.01,
        ),
    )
    leaked_before_test_cleanup: list[asyncio.Task] = []
    try:
        await asyncio.wait_for(started.wait(), timeout=1.0)
        scan_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await scan_task
        leaked_before_test_cleanup = [task for task in probe_tasks if not task.done()]
    finally:
        # Keep this regression test itself leak-free even while it exposes a
        # missing production cleanup path.
        for task in probe_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*probe_tasks, return_exceptions=True)

    assert leaked_before_test_cleanup == []
    assert sorted(cancelled_ports) == [49152, 49153, 49154]


async def test_fallback_endpoints_exclude_lockdownd_port() -> None:
    async def fake_port_scan(_ip: str) -> list[int]:
        return [62078, 50100, 62078]

    async def fake_discover() -> list[dict]:
        return [
            {"ip": "192.0.2.10", "port": 62078},
            {"ip": "192.0.2.11", "port": 50101},
        ]

    result = await tunnel_discovery.find_fallback_endpoints(
        "192.0.2.10",
        port_scan=fake_port_scan,
        discover=fake_discover,
    )

    assert result == [
        ("192.0.2.10", 50100),
        ("192.0.2.11", 50101),
    ]

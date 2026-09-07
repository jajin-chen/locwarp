"""Concurrent failures must not retire the provider that just recovered."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import services.location_service as module


async def test_concurrent_failed_updates_share_recovered_provider(monkeypatch):
    old = MagicMock()
    fresh = MagicMock()
    both_failed = asyncio.Event()
    calls = 0

    async def fail(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            both_failed.set()
        await both_failed.wait()
        raise OSError("old channel closed")

    broken = MagicMock(set=AsyncMock(side_effect=fail))
    ready = MagicMock(connect=AsyncMock(), set=AsyncMock())
    monkeypatch.setattr(module, "LocationSimulation", MagicMock(return_value=ready))
    factory = AsyncMock(return_value=fresh)
    service = module.DvtLocationService(old, dvt_factory=factory)
    service._location_sim = broken

    await asyncio.wait_for(asyncio.gather(service.set(1, 2), service.set(3, 4)), 2)

    factory.assert_awaited_once()
    old.__aexit__.assert_awaited_once()
    fresh.__aexit__.assert_not_awaited()
    assert ready.set.await_count == 2


async def test_instrument_initialization_waits_for_provider_replacement(monkeypatch):
    closing = asyncio.Event()
    release = asyncio.Event()
    old = MagicMock()
    fresh = MagicMock()

    async def close(*args):
        closing.set()
        await release.wait()

    old.__aexit__ = AsyncMock(side_effect=close)
    ready = MagicMock(connect=AsyncMock())
    constructor = MagicMock(return_value=ready)
    monkeypatch.setattr(module, "LocationSimulation", constructor)
    service = module.DvtLocationService(old, dvt_factory=AsyncMock(return_value=fresh))
    recovery = asyncio.create_task(service._reconnect())
    initialize = None
    try:
        await asyncio.wait_for(closing.wait(), 1)
        initialize = asyncio.create_task(service._ensure_instrument())
        await asyncio.sleep(0)
        constructor.assert_not_called()
    finally:
        release.set()
        await asyncio.gather(recovery, *([initialize] if initialize else []))
    constructor.assert_called_once_with(fresh)


@pytest.mark.parametrize("operation", ["set", "clear"])
@pytest.mark.parametrize("failure_stage", ["connect", "command"])
async def test_queued_operation_recovers_the_provider_it_actually_uses(
    monkeypatch, operation, failure_stage,
):
    closing = asyncio.Event()
    release = asyncio.Event()
    old, intermediate, fresh = MagicMock(), MagicMock(), MagicMock()

    async def close(*args):
        closing.set()
        await release.wait()

    old.__aexit__ = AsyncMock(side_effect=close)
    broken = MagicMock(connect=AsyncMock(), set=AsyncMock(), clear=AsyncMock())
    failing = broken.connect if failure_stage == "connect" else getattr(broken, operation)
    failing.side_effect = OSError("new channel also failed")
    ready = MagicMock(connect=AsyncMock(), set=AsyncMock(), clear=AsyncMock())
    monkeypatch.setattr(module, "LocationSimulation", MagicMock(side_effect=[broken, ready]))
    factory = AsyncMock(side_effect=[intermediate, fresh])
    service = module.DvtLocationService(old, dvt_factory=factory)
    service._active = True
    recovery = asyncio.create_task(service._reconnect())
    pending = None
    try:
        await asyncio.wait_for(closing.wait(), 1)
        pending = asyncio.create_task(service.set(1, 2) if operation == "set" else service.clear())
        await asyncio.sleep(0)
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(recovery, *([pending] if pending else [])), 2)
    assert factory.await_count == 2
    intermediate.__aexit__.assert_awaited_once()
    getattr(ready, operation).assert_awaited_once()

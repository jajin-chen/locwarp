"""Only connected DVT instruments may be shared by location requests."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import services.location_service as module


async def test_failed_instrument_connection_is_not_cached(monkeypatch):
    failed = MagicMock(connect=AsyncMock(side_effect=RuntimeError("channel unavailable")))
    ready = MagicMock(connect=AsyncMock(), set=AsyncMock())
    factory = MagicMock(side_effect=[failed, ready])
    monkeypatch.setattr(module, "LocationSimulation", factory)
    service = module.DvtLocationService(MagicMock())

    with pytest.raises(RuntimeError, match="channel unavailable"):
        await service.set(1.0, 2.0)

    assert service._location_sim is None
    assert service._active is False
    failed.set.assert_not_called()

    await service.set(3.0, 4.0)

    ready.connect.assert_awaited_once()
    ready.set.assert_awaited_once_with(3.0, 4.0)
    assert service._location_sim is ready


async def test_concurrent_requests_wait_for_instrument_connection(monkeypatch):
    connecting = asyncio.Event()
    release = asyncio.Event()

    async def connect():
        connecting.set()
        await release.wait()

    instrument = MagicMock(connect=AsyncMock(side_effect=connect), set=AsyncMock())
    factory = MagicMock(return_value=instrument)
    monkeypatch.setattr(module, "LocationSimulation", factory)
    service = module.DvtLocationService(MagicMock())
    first = asyncio.create_task(service.set(1.0, 2.0))
    second = None
    try:
        await asyncio.wait_for(connecting.wait(), timeout=1)
        second = asyncio.create_task(service.set(3.0, 4.0))
        await asyncio.sleep(0)
        assert service._location_sim is None
        instrument.set.assert_not_called()
        factory.assert_called_once()
    finally:
        release.set()
        await asyncio.gather(first, *([second] if second is not None else []))

    instrument.connect.assert_awaited_once()
    assert instrument.set.await_count == 2

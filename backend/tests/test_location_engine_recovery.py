"""Lazy engine recovery must retain the requested device and transport."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

import main
from api.location import _engine


@pytest.fixture
def state(monkeypatch):
    primary = object()
    engines = {"primary": primary}
    dm = SimpleNamespace(
        _connections={"wifi": object()},
        full_reconnect=AsyncMock(return_value=True),
        connect=AsyncMock(),
        disconnect=AsyncMock(),
    )
    state = SimpleNamespace(
        simulation_engine=primary,
        get_engine=engines.get,
        create_engine_for_device=AsyncMock(),
        device_manager=dm,
        engines=engines,
    )
    monkeypatch.setattr(main, "app_state", state)
    return state


@pytest.mark.asyncio
async def test_rebuild_returns_requested_device_instead_of_primary(state):
    expected = object()

    async def rebuild(udid):
        state.engines[udid] = expected

    state.create_engine_for_device.side_effect = rebuild
    assert await _engine("wifi") is expected
    state.device_manager.full_reconnect.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_rebuild_uses_transport_aware_recovery(state):
    expected = object()

    async def rebuild(udid):
        if state.create_engine_for_device.await_count == 1:
            raise RuntimeError("stale DVT")
        state.engines[udid] = expected

    state.create_engine_for_device.side_effect = rebuild
    assert await _engine("wifi") is expected
    state.device_manager.full_reconnect.assert_awaited_once_with("wifi")
    state.device_manager.disconnect.assert_not_awaited()
    state.device_manager.connect.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_requested_engine_never_returns_primary(state):
    state.device_manager.full_reconnect.return_value = False
    with pytest.raises(HTTPException) as exc:
        await _engine("wifi")
    assert exc.value.status_code == 400
    state.device_manager.full_reconnect.assert_awaited_once_with("wifi")
    state.create_engine_for_device.assert_awaited_once_with("wifi")


@pytest.mark.asyncio
async def test_successful_reconnect_without_engine_fails_closed(state):
    with pytest.raises(HTTPException) as exc:
        await _engine("wifi")
    assert exc.value.status_code == 400
    assert state.create_engine_for_device.await_count == 2

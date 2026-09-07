"""Late failures from retired requests must not disconnect replacement devices."""

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api import location, websocket
from services.location_service import DeviceLostError


def engine_and_connection():
    service = object()
    engine = SimpleNamespace(
        location_service=service, _stop_event=asyncio.Event(),
        _pause_event=asyncio.Event(), _active_task=None,
    )
    return engine, SimpleNamespace(location_service=service)


@pytest.fixture
def state(monkeypatch):
    engine, connection = engine_and_connection()
    dm = SimpleNamespace(_connections={"phone": connection})

    async def disconnect(udid, expected):
        if dm._connections.get(udid) is not expected:
            return False
        dm._connections.pop(udid)
        return True

    dm.disconnect_if_current = AsyncMock(side_effect=disconnect)
    state = SimpleNamespace(
        device_manager=dm, simulation_engines={"phone": engine}, _primary_udid="phone",
    )
    monkeypatch.setitem(sys.modules, "main", SimpleNamespace(app_state=state))
    monkeypatch.setattr(websocket, "broadcast", AsyncMock())
    monkeypatch.setattr(location, "_engine", AsyncMock(side_effect=lambda _: state.simulation_engines["phone"]))
    return state


async def test_stale_request_cleanup_preserves_replacement(state):
    lease = location._LocationActionLease("phone")
    old_engine = await lease.resolve_engine()
    replacement, connection = engine_and_connection()
    state.simulation_engines["phone"] = replacement
    state.device_manager._connections["phone"] = connection

    result = await location._handle_device_lost(DeviceLostError("old channel"), "phone", lease=lease)

    assert result.status_code == 503
    assert state.simulation_engines["phone"] is replacement
    assert state.device_manager._connections["phone"] is connection
    assert not replacement._stop_event.is_set()
    assert not old_engine._stop_event.is_set()
    state.device_manager.disconnect_if_current.assert_not_called()
    websocket.broadcast.assert_not_called()


async def test_current_failed_request_cleans_only_owned_device(state):
    other_engine, other_connection = engine_and_connection()
    state.simulation_engines["other"] = other_engine
    state.device_manager._connections["other"] = other_connection
    lease = location._LocationActionLease("phone")
    engine = await lease.resolve_engine()

    await location._handle_device_lost(DeviceLostError("dead channel"), "phone", lease=lease)

    assert engine._stop_event.is_set()
    assert engine._pause_event.is_set()
    assert state.simulation_engines == {"other": other_engine}
    assert state.device_manager._connections == {"other": other_connection}
    assert state._primary_udid == "other"
    state.device_manager.disconnect_if_current.assert_awaited_once_with("phone", lease.connection)
    assert websocket.broadcast.await_args.args[1]["udids"] == ["phone"]


async def test_replacement_during_close_survives_without_disconnect_event(state):
    lease = location._LocationActionLease("phone")
    await lease.resolve_engine()
    replacement, connection = engine_and_connection()

    async def replace_during_close(udid, expected):
        assert state.device_manager._connections[udid] is expected
        state.device_manager._connections[udid] = connection
        state.simulation_engines[udid] = replacement
        state._primary_udid = udid
        return True

    state.device_manager.disconnect_if_current.side_effect = replace_during_close
    await location._handle_device_lost(DeviceLostError("dead channel"), "phone", lease=lease)

    assert state.simulation_engines["phone"] is replacement
    assert state.device_manager._connections["phone"] is connection
    assert state._primary_udid == "phone"
    websocket.broadcast.assert_not_called()


async def test_retry_refreshes_lease_and_mismatched_service_is_unowned(state):
    lease = location._LocationActionLease("phone")
    await lease.resolve_engine()
    replacement, connection = engine_and_connection()
    state.simulation_engines["phone"] = replacement
    state.device_manager._connections["phone"] = connection
    assert await lease.resolve_engine() is replacement
    assert lease.connection is connection

    state.device_manager._connections["phone"] = engine_and_connection()[1]
    await lease.resolve_engine()
    assert lease.connection is None
    await location._handle_device_lost(DeviceLostError("retired engine"), "phone", lease=lease)
    state.device_manager.disconnect_if_current.assert_not_called()


async def test_restore_failed_recovery_does_not_clean_concurrent_replacement(state):
    state.simulation_engines["phone"].restore = AsyncMock(side_effect=DeviceLostError("old channel"))
    replacement, connection = engine_and_connection()

    async def failed_recovery(_, *, expected):
        # Another request has succeeded while this request's recovery failed.
        assert expected is state.device_manager._connections["phone"]
        state.simulation_engines["phone"] = replacement
        state.device_manager._connections["phone"] = connection
        return False

    state.device_manager.full_reconnect = AsyncMock(side_effect=failed_recovery)
    with pytest.raises(location.HTTPException) as raised:
        await location.restore("phone")

    assert raised.value.status_code == 503
    assert state.simulation_engines["phone"] is replacement
    assert state.device_manager._connections["phone"] is connection
    state.device_manager.disconnect_if_current.assert_not_called()
    websocket.broadcast.assert_not_called()


async def test_full_reconnect_rechecks_expected_after_waiting_for_lock():
    from core.device_manager import DeviceManager, _ActiveConnection

    manager = DeviceManager()
    old = _ActiveConnection(udid="phone", lockdown=object(), ios_version="26.6.1")
    replacement = _ActiveConnection(udid="phone", lockdown=object(), ios_version="26.6.1")
    manager._connections["phone"] = old
    manager.disconnect = AsyncMock()
    manager.disconnect_if_current = AsyncMock()
    manager.connect = AsyncMock()

    await manager._lock.acquire()
    task = asyncio.create_task(manager.full_reconnect("phone", expected=old))
    try:
        await asyncio.sleep(0)
        assert not task.done()
        manager._connections["phone"] = replacement
    finally:
        manager._lock.release()

    assert await task is False
    assert manager._connections["phone"] is replacement
    manager.disconnect.assert_not_called()
    manager.disconnect_if_current.assert_not_called()
    manager.connect.assert_not_called()


async def test_delayed_failure_retries_replacement_without_reconnecting(state):
    replacement, connection = engine_and_connection()
    replacement.restore = AsyncMock()

    async def stale_failure():
        state.simulation_engines["phone"] = replacement
        state.device_manager._connections["phone"] = connection
        raise DeviceLostError("delayed old failure")

    state.simulation_engines["phone"].restore = AsyncMock(side_effect=stale_failure)
    state.device_manager.full_reconnect = AsyncMock()

    assert await location.restore("phone") == {"status": "restored"}

    replacement.restore.assert_awaited_once()
    state.device_manager.full_reconnect.assert_not_called()
    state.device_manager.disconnect_if_current.assert_not_called()
    websocket.broadcast.assert_not_called()


async def test_delayed_failure_without_coherent_replacement_does_not_reconnect(state):
    replacement, connection = engine_and_connection()
    replacement.restore = AsyncMock()

    async def stale_failure():
        state.simulation_engines["phone"] = replacement
        # The replacement engine does not yet belong to this connection.
        state.device_manager._connections["phone"] = engine_and_connection()[1]
        raise DeviceLostError("delayed old failure")

    state.simulation_engines["phone"].restore = AsyncMock(side_effect=stale_failure)
    state.device_manager.full_reconnect = AsyncMock()

    with pytest.raises(location.HTTPException) as raised:
        await location.restore("phone")

    assert raised.value.status_code == 503
    replacement.restore.assert_not_called()
    state.device_manager.full_reconnect.assert_not_called()
    state.device_manager.disconnect_if_current.assert_not_called()

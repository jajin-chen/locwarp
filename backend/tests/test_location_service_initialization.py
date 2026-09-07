"""Location initialization belongs to exactly one connection lease."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import core.device_manager as dm_module
from core.device_manager import DeviceManager, _ActiveConnection
from services.location_service import DeviceLostError

pytestmark = pytest.mark.asyncio


def connection(udid="phone"):
    return _ActiveConnection(udid, object(), "17.5", connection_type="Network")


async def test_concurrent_initializers_share_one_service(monkeypatch):
    dm = DeviceManager()
    dm._connections["phone"] = connection()
    entered, release = asyncio.Event(), asyncio.Event()
    service = object()

    async def create(conn):
        entered.set()
        await release.wait()
        return service

    factory = AsyncMock(side_effect=create)
    monkeypatch.setattr(dm, "_create_dvt_location_service", factory)
    first = asyncio.create_task(dm.get_location_service("phone"))
    await entered.wait()
    second = asyncio.create_task(dm.get_location_service("phone"))
    await asyncio.sleep(0)
    release.set()
    assert await asyncio.gather(first, second) == [service, service]
    factory.assert_awaited_once()


async def test_replacement_during_init_closes_old_provider_without_publishing(monkeypatch):
    dm = DeviceManager()
    old, replacement = connection(), connection()
    dm._connections["phone"] = old
    entered, release = asyncio.Event(), asyncio.Event()
    orphan = SimpleNamespace(__aexit__=AsyncMock())
    healthy = object()
    replacement.location_service = healthy

    async def create(conn):
        entered.set()
        await release.wait()
        conn.dvt_provider = orphan
        return object()

    monkeypatch.setattr(dm, "_create_dvt_location_service", create)
    pending = asyncio.create_task(dm.get_location_service("phone"))
    await entered.wait()
    async with dm._lock:
        dm._connections["phone"] = replacement
    assert await dm.get_location_service("phone") is healthy
    release.set()
    with pytest.raises(DeviceLostError):
        await pending
    assert old.location_service is None
    assert old.dvt_provider is None
    orphan.__aexit__.assert_awaited_once()
    assert replacement.location_service is healthy


async def test_other_device_initialization_is_not_blocked(monkeypatch):
    dm = DeviceManager()
    dm._connections.update(phone=connection(), other=connection("other"))
    entered, release = asyncio.Event(), asyncio.Event()
    healthy = object()

    async def create(conn):
        if conn.udid == "phone":
            entered.set()
            await release.wait()
        return healthy

    monkeypatch.setattr(dm, "_create_dvt_location_service", create)
    pending = asyncio.create_task(dm.get_location_service("phone"))
    await entered.wait()
    try:
        assert await asyncio.wait_for(dm.get_location_service("other"), 1) is healthy
    finally:
        release.set()
        await pending


async def test_cancelled_provider_enter_is_closed(monkeypatch):
    dm = DeviceManager()
    conn = connection()
    dm._connections["phone"] = conn
    entered, release = asyncio.Event(), asyncio.Event()

    async def enter():
        entered.set()
        await release.wait()

    provider = SimpleNamespace(__aenter__=enter, __aexit__=AsyncMock())
    monkeypatch.setattr(dm_module, "DvtProvider", lambda _: provider)
    monkeypatch.setattr(dm, "_ensure_personalized_ddi_mounted", AsyncMock())
    pending = asyncio.create_task(dm.get_location_service("phone"))
    await entered.wait()
    pending.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    provider.__aexit__.assert_awaited_once()
    assert conn.location_service is None


async def test_cancel_during_real_provider_handshake_drains_dtx(monkeypatch):
    from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider

    dm = DeviceManager()
    conn = connection()
    conn.lockdown = SimpleNamespace(product_version="17.5")
    dm._connections["phone"] = conn
    entered, release = asyncio.Event(), asyncio.Event()

    async def connect():
        entered.set()
        await release.wait()

    dtx = SimpleNamespace(
        connect=connect, aclose=AsyncMock(), register_services=lambda *args: None,
    )
    monkeypatch.setattr(DvtProvider, "_open_dtx_connection", AsyncMock(return_value=dtx))
    monkeypatch.setattr(dm, "_ensure_personalized_ddi_mounted", AsyncMock())
    pending = asyncio.create_task(dm.get_location_service("phone"))
    await entered.wait()
    pending.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    dtx.aclose.assert_awaited_once()
    assert conn.dvt_provider is None
    assert conn.location_service is None


async def test_concurrent_engine_builder_preserves_first_engine(monkeypatch):
    import main

    entered, release = asyncio.Event(), asyncio.Event()
    engines = {}
    existing = object()

    async def get_service(udid):
        entered.set()
        await release.wait()
        return object()

    state = SimpleNamespace(
        simulation_engines=engines,
        device_manager=SimpleNamespace(get_location_service=get_service),
        _primary_udid="phone",
    )
    pending = asyncio.create_task(main.AppState.create_engine_for_device(state, "phone"))
    await entered.wait()
    engines["phone"] = existing
    release.set()
    await pending
    assert engines["phone"] is existing

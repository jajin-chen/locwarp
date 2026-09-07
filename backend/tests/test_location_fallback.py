"""DVT failures must not cache an unusable legacy location service."""

from unittest.mock import AsyncMock, MagicMock

import pytest

import core.device_manager as module
from core.device_manager import DeviceManager, _ActiveConnection
from services.location_service import LegacyLocationService


def failed_dvt_manager(monkeypatch):
    manager = DeviceManager()
    manager._ensure_personalized_ddi_mounted = AsyncMock()
    failure = TimeoutError("DTX capability handshake timed out")
    dvt = MagicMock()
    dvt.__aenter__ = AsyncMock(side_effect=failure)
    monkeypatch.setattr(module, "DvtProvider", MagicMock(return_value=dvt))
    return manager, failure


async def test_rsd_only_dvt_failure_does_not_cache_legacy(monkeypatch):
    manager, failure = failed_dvt_manager(monkeypatch)
    rsd = MagicMock(spec=module.RemoteServiceDiscoveryService)
    conn = _ActiveConnection(udid="wifi-phone", lockdown=rsd, ios_version="26.6.1")
    manager._connections[conn.udid] = conn
    legacy = MagicMock()
    monkeypatch.setattr(module, "LegacyLocationService", legacy)

    with pytest.raises(TimeoutError) as raised:
        await manager.get_location_service(conn.udid)

    assert raised.value is failure
    assert conn.location_service is None
    legacy.assert_not_called()
    rsd.start_lockdown_developer_service.assert_not_called()


async def test_direct_lockdown_fallback_is_probed_without_location_change(monkeypatch):
    manager, _ = failed_dvt_manager(monkeypatch)
    direct = MagicMock()
    probe = MagicMock(close=AsyncMock())
    direct.start_lockdown_developer_service = AsyncMock(return_value=probe)
    conn = _ActiveConnection(
        udid="usb-phone", lockdown=MagicMock(spec=module.RemoteServiceDiscoveryService),
        usbmux_lockdown=direct, ios_version="26.6.1",
    )
    manager._connections[conn.udid] = conn

    service = await manager.get_location_service(conn.udid)

    assert isinstance(service, LegacyLocationService)
    assert service._lockdown is direct
    assert service._active is False
    direct.start_lockdown_developer_service.assert_awaited_once_with(
        module.DtSimulateLocation.SERVICE_NAME
    )
    probe.close.assert_awaited_once()
    probe.sendall.assert_not_called()


async def test_unavailable_direct_fallback_preserves_dvt_failure(monkeypatch):
    manager, failure = failed_dvt_manager(monkeypatch)
    direct = MagicMock()
    direct.start_lockdown_developer_service = AsyncMock(side_effect=RuntimeError("No such service"))
    conn = _ActiveConnection(udid="usb-phone", lockdown=direct, ios_version="26.6.1")
    manager._connections[conn.udid] = conn

    with pytest.raises(TimeoutError) as raised:
        await manager.get_location_service(conn.udid)

    assert raised.value is failure
    assert conn.location_service is None

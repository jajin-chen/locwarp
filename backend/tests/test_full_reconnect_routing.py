"""Tests for the WiFi-vs-USB dispatch logic in DeviceManager.full_reconnect.

Regression coverage for a live incident (2026-07-30 21:11): full_reconnect
is invoked as the API-layer safety net after DeviceLostError, which means
the connection record for the device has usually already been removed by
the time it runs. The old dispatch only checked
``conn.connection_type == "Network"``, so a WiFi device whose connection
record was already gone (conn is None -> conn_type is None) fell through
to the USB fallback. That fallback disconnects first and then tries a USB
lockdown connection that can never succeed on a machine without usbmuxd,
destroying the recoverable tunnel state in the process.
"""

import core.device_manager as device_manager_module
from core.device_manager import DeviceManager, _ActiveConnection, _should_use_wifi_recovery


class _FakeRunner:
    def __init__(self, target_ip: str | None = "192.168.1.50", target_port: int | None = 51000) -> None:
        self.target_ip = target_ip
        self.target_port = target_port


# ---------------------------------------------------------------------
# _should_use_wifi_recovery: pure dispatch logic
# ---------------------------------------------------------------------

def test_network_conn_type_always_uses_wifi_recovery() -> None:
    assert _should_use_wifi_recovery("Network", {}, "udid-1") is True
    assert _should_use_wifi_recovery("Network", {"udid-1": _FakeRunner()}, "udid-1") is True


def test_none_conn_type_with_live_tunnel_runner_uses_wifi_recovery() -> None:
    """Core regression case: connection record already gone, but a live
    WiFi tunnel runner for this udid proves it was a WiFi device."""
    tunnels = {"udid-1": _FakeRunner()}
    assert _should_use_wifi_recovery(None, tunnels, "udid-1") is True


def test_none_conn_type_without_tunnel_runner_falls_back_to_usb() -> None:
    assert _should_use_wifi_recovery(None, {}, "udid-1") is False
    assert _should_use_wifi_recovery(None, {"other-udid": _FakeRunner()}, "udid-1") is False


def test_usb_conn_type_never_uses_wifi_recovery() -> None:
    assert _should_use_wifi_recovery("USB", {"udid-1": _FakeRunner()}, "udid-1") is False


# ---------------------------------------------------------------------
# full_reconnect: end-to-end dispatch through the real method
# ---------------------------------------------------------------------

async def test_full_reconnect_network_conn_uses_tunnel_restart(monkeypatch) -> None:
    dm = DeviceManager()
    udid = "00008103-000A74441499401E"
    dm._connections[udid] = _ActiveConnection(
        udid=udid, lockdown=object(), ios_version="17.0", connection_type="Network",
    )

    runner = _FakeRunner()
    restart_calls = []

    async def fake_restart(u, ip, port, snapshot, orig_runner):
        restart_calls.append((u, ip, port, snapshot, orig_runner))
        return True

    import services.tunnel_manager as tunnel_manager_module
    monkeypatch.setitem(tunnel_manager_module._tunnels, udid, runner)
    monkeypatch.setattr(tunnel_manager_module, "_attempt_tunnel_restart", fake_restart)

    connect_calls = []
    disconnect_calls = []
    monkeypatch.setattr(dm, "connect", lambda u: connect_calls.append(u))
    monkeypatch.setattr(dm, "disconnect", lambda u: disconnect_calls.append(u))

    result = await dm.full_reconnect(udid)

    assert result is True
    assert restart_calls == [(udid, runner.target_ip, runner.target_port, None, runner)]
    assert connect_calls == []
    assert disconnect_calls == []


async def test_full_reconnect_missing_conn_record_with_live_tunnel_uses_wifi_path(monkeypatch) -> None:
    """The regression: conn record already removed (conn_type is None),
    but a live tunnel runner exists -> must take the WiFi path, not USB."""
    dm = DeviceManager()
    udid = "00008103-000A74441499401E"
    assert udid not in dm._connections  # simulates cleanup having already run

    runner = _FakeRunner()
    restart_calls = []

    async def fake_restart(u, ip, port, snapshot, orig_runner):
        restart_calls.append((u, ip, port, snapshot, orig_runner))
        return True

    import services.tunnel_manager as tunnel_manager_module
    monkeypatch.setitem(tunnel_manager_module._tunnels, udid, runner)
    monkeypatch.setattr(tunnel_manager_module, "_attempt_tunnel_restart", fake_restart)

    connect_calls = []
    disconnect_calls = []
    monkeypatch.setattr(dm, "connect", lambda u: connect_calls.append(u))
    monkeypatch.setattr(dm, "disconnect", lambda u: disconnect_calls.append(u))

    result = await dm.full_reconnect(udid)

    assert result is True
    assert restart_calls == [(udid, runner.target_ip, runner.target_port, None, runner)]
    assert connect_calls == []
    assert disconnect_calls == []


async def test_full_reconnect_missing_conn_record_without_tunnel_uses_usb_path(monkeypatch) -> None:
    """No connection record and no tunnel runner: nothing proves this was
    a WiFi device, so the USB fallback is the correct (unchanged) behavior."""
    dm = DeviceManager()
    udid = "00008103-000A74441499401E"
    assert udid not in dm._connections

    import services.tunnel_manager as tunnel_manager_module
    monkeypatch.delitem(tunnel_manager_module._tunnels, udid, raising=False)

    restart_calls = []

    async def fake_restart(*args, **kwargs):
        restart_calls.append(args)
        return True

    monkeypatch.setattr(tunnel_manager_module, "_attempt_tunnel_restart", fake_restart)

    connect_calls = []
    disconnect_calls = []

    async def fake_connect(u):
        connect_calls.append(u)
        dm._connections[u] = _ActiveConnection(
            udid=u, lockdown=object(), ios_version="17.0", connection_type="USB",
        )

    async def fake_disconnect(u):
        disconnect_calls.append(u)

    monkeypatch.setattr(dm, "connect", fake_connect)
    monkeypatch.setattr(dm, "disconnect", fake_disconnect)

    result = await dm.full_reconnect(udid)

    assert result is True
    assert restart_calls == []
    assert disconnect_calls == [udid]
    assert connect_calls == [udid]

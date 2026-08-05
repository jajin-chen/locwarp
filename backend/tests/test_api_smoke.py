"""API smoke tests — boot the FastAPI app in-process (no real iPhone) and
verify the core endpoints respond with the right status codes and shapes.

Uses httpx.ASGITransport so the lifespan (device auto-connect + watchdogs)
is NOT started; a fake engine + fake device manager stand in for hardware.
"""

from __future__ import annotations

import httpx
import pytest

from fakes import FakeLocationService

TEST_UDID = "TEST-UDID-0001"


@pytest.fixture
async def client():
    from main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def fake_engine(monkeypatch):
    """Register a real SimulationEngine backed by a FakeLocationService as
    the primary engine, so /api/location/* endpoints resolve instantly
    instead of entering the 10x1s device-discovery retry loop."""
    from core.simulation_engine import SimulationEngine
    from main import app_state

    svc = FakeLocationService()
    engine = SimulationEngine(svc)
    monkeypatch.setitem(app_state.simulation_engines, TEST_UDID, engine)
    monkeypatch.setattr(app_state, "_primary_udid", TEST_UDID)
    return engine


@pytest.fixture
def fake_device_manager(monkeypatch):
    """Stub discover_devices so /api/device/list never touches usbmuxd."""
    from models.schemas import DeviceInfo
    from main import app_state

    device = DeviceInfo(
        udid=TEST_UDID,
        name="Test iPhone",
        ios_version="17.5",
        connection_type="USB",
        is_connected=True,
    )

    async def fake_discover():
        return [device]

    monkeypatch.setattr(app_state.device_manager, "discover_devices", fake_discover)
    return device


# ── Root ─────────────────────────────────────────────────────────────


async def test_root_reports_running(client):
    resp = await client.get("/")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "LocWarp"
    assert body["status"] == "running"
    pos = body["initial_position"]
    assert -90 <= pos["lat"] <= 90
    assert -180 <= pos["lng"] <= 180


# ── Device endpoints ─────────────────────────────────────────────────


async def test_device_list_returns_devices(client, fake_device_manager):
    resp = await client.get("/api/device/list")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert body[0]["udid"] == TEST_UDID
    assert body[0]["is_connected"] is True


async def test_wifi_tunnel_status_shape(client):
    resp = await client.get("/api/device/wifi/tunnel/status")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["tunnels"], list)
    assert isinstance(body["running"], bool)


async def test_wifi_keepalive_get_returns_bool(client):
    resp = await client.get("/api/device/wifi/tunnel/keepalive")
    assert resp.status_code == 200
    assert isinstance(resp.json()["enabled"], bool)


# ── Location endpoints ───────────────────────────────────────────────


async def test_location_status_idle(client, fake_engine):
    resp = await client.get("/api/location/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "idle"
    assert body["current_position"] is None
    assert body["is_paused"] is False


async def test_teleport_pushes_position_and_updates_status(client, fake_engine):
    resp = await client.post(
        "/api/location/teleport", json={"lat": 25.033, "lng": 121.5654},
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "lat": 25.033, "lng": 121.5654}
    # The coordinate reached the (fake) device untouched.
    assert fake_engine.location_service.positions == [(25.033, 121.5654)]

    status = (await client.get("/api/location/status")).json()
    assert status["current_position"] == {"lat": 25.033, "lng": 121.5654}


async def test_teleport_rejects_out_of_range_lat(client, fake_engine):
    resp = await client.post(
        "/api/location/teleport", json={"lat": 999, "lng": 121.5654},
    )
    assert resp.status_code == 422
    assert fake_engine.location_service.positions == []


async def test_cooldown_status_shape(client):
    resp = await client.get("/api/location/cooldown/status")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) >= {
        "enabled", "is_active", "remaining_seconds", "total_seconds", "distance_km",
    }
    assert body["is_active"] is False


async def test_coord_format_get(client):
    resp = await client.get("/api/location/settings/coord-format")
    assert resp.status_code == 200
    assert resp.json()["format"] in ("dd", "dms", "dm")


async def test_location_debug_reports_engine(client, fake_engine):
    resp = await client.get("/api/location/debug")
    assert resp.status_code == 200
    body = resp.json()
    assert body["engine"] == "SimulationEngine"
    assert body["location_service"] == "FakeLocationService"

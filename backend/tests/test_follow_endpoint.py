"""Endpoint + isolation tests for /ws/follow.

The isolation test is the contract that matters most: a blowing-up
follower pipeline must NEVER break the iOS event pipeline
(broadcast + update_last_position).
"""

from __future__ import annotations

import asyncio
import json

import pytest
from starlette.testclient import TestClient

from api import follow
from fakes import FakeLocationService

TEST_UDID = "TEST-UDID-0001"


@pytest.fixture(autouse=True)
def clean_followers():
    follow._followers.clear()
    yield
    follow._followers.clear()


@pytest.fixture
def primary(monkeypatch):
    from main import app_state

    monkeypatch.setattr(app_state, "_primary_udid", TEST_UDID)
    return app_state


def test_ws_follow_sends_hello_on_connect(primary):
    from main import app

    # NOTE: deliberately NOT `with TestClient(app)` — entering the client
    # context runs the lifespan (real device discovery). websocket_connect
    # alone skips lifespan, same spirit as the ASGITransport smoke tests.
    client = TestClient(app)
    with client.websocket_connect("/ws/follow") as ws:
        msg = json.loads(ws.receive_text())
        assert msg["type"] == "hello"
        assert msg["app"] == "locwarp"
        assert msg["protocol"] == 1
        assert msg["udid"] == TEST_UDID


def test_ws_follow_ignores_inbound_text(primary):
    from main import app

    client = TestClient(app)
    with client.websocket_connect("/ws/follow") as ws:
        ws.receive_text()  # hello
        ws.send_text('{"type":"evil_command"}')  # must be ignored, not crash
        ws.send_text("not json at all")
    # surviving the context exit without server error IS the assertion


async def test_engine_event_reaches_follower(monkeypatch, primary):
    """End-to-end: engine _emit → event_callback → forward → follower ws."""
    from main import app_state

    sent: list[tuple[str, dict]] = []

    async def fake_broadcast(event_type, data):
        sent.append((event_type, data))

    monkeypatch.setattr("api.websocket.broadcast", fake_broadcast)

    async def fake_get_location_service(udid):
        return FakeLocationService()

    monkeypatch.setattr(
        app_state.device_manager, "get_location_service", fake_get_location_service
    )
    app_state.simulation_engines.pop(TEST_UDID, None)
    await app_state.create_engine_for_device(TEST_UDID)
    engine = app_state.simulation_engines[TEST_UDID]

    class FakeWs:
        def __init__(self):
            self.sent = []

        async def send_text(self, text):
            self.sent.append(text)

    fw = FakeWs()
    follow._followers.append(fw)

    await engine._emit("position_update", {"lat": 25.0, "lng": 121.0})

    assert json.loads(fw.sent[0]) == {"type": "position", "lat": 25.0, "lng": 121.0}
    app_state.simulation_engines.pop(TEST_UDID, None)


async def test_follow_failure_never_breaks_ios_pipeline(monkeypatch, primary):
    """THE isolation regression test: follow.forward raising must not stop
    broadcast nor update_last_position."""
    from main import app_state

    sent: list[tuple[str, dict]] = []

    async def fake_broadcast(event_type, data):
        sent.append((event_type, data))

    monkeypatch.setattr("api.websocket.broadcast", fake_broadcast)

    async def bomb(event_type, data):
        raise RuntimeError("follower pipeline is on fire")

    monkeypatch.setattr(follow, "forward", bomb)

    async def fake_get_location_service(udid):
        return FakeLocationService()

    monkeypatch.setattr(
        app_state.device_manager, "get_location_service", fake_get_location_service
    )
    app_state.simulation_engines.pop(TEST_UDID, None)
    await app_state.create_engine_for_device(TEST_UDID)
    engine = app_state.simulation_engines[TEST_UDID]

    await engine._emit("position_update", {"lat": 25.5, "lng": 121.5})

    assert sent, "broadcast must still run when follow.forward raises"
    assert sent[0][0] == "position_update"
    assert app_state._last_position == {"lat": 25.5, "lng": 121.5}
    app_state.simulation_engines.pop(TEST_UDID, None)


async def test_forward_drops_follower_that_never_responds(monkeypatch, primary):
    """A wedged follower socket whose send_text() hangs forever must not
    block forward() — it gets dropped once the per-send timeout fires."""
    from main import app_state

    class WedgedWs:
        def __init__(self):
            self.dropped_after = None

        async def send_text(self, text):
            await asyncio.sleep(3600)  # never returns within the timeout

    wedged = WedgedWs()
    follow._followers.append(wedged)

    await asyncio.wait_for(
        follow.forward(
            "position_update", {"lat": 25.0, "lng": 121.0, "udid": app_state._primary_udid}
        ),
        timeout=5.0,
    )

    assert wedged not in follow._followers

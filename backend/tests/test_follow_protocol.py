"""Unit tests for the /ws/follow protocol translation and fan-out.

The follower feed is a read-only tap on the engine event stream — these
tests pin the translation rules (event filtering, udid gating) and the
never-raise guarantee of forward().
"""

from __future__ import annotations

import json

import pytest

from api import follow

PRIMARY = "PRIMARY-UDID"


# ── translate ────────────────────────────────────────────

def test_position_update_translates_to_position():
    msg = follow.translate(
        "position_update", {"lat": 25.03, "lng": 121.56, "udid": PRIMARY}, PRIMARY
    )
    assert msg == {"type": "position", "lat": 25.03, "lng": 121.56}


def test_teleport_translates_to_teleport():
    msg = follow.translate(
        "teleport", {"lat": 24.0, "lng": 120.0, "udid": PRIMARY}, PRIMARY
    )
    assert msg == {"type": "teleport", "lat": 24.0, "lng": 120.0}


def test_state_change_translates_to_sim_state():
    msg = follow.translate(
        "state_change", {"state": "navigating", "udid": PRIMARY}, PRIMARY
    )
    assert msg == {"type": "sim_state", "state": "navigating"}


def test_non_primary_udid_is_dropped():
    msg = follow.translate(
        "position_update", {"lat": 25.0, "lng": 121.0, "udid": "OTHER"}, PRIMARY
    )
    assert msg is None


def test_no_primary_device_drops_everything():
    msg = follow.translate(
        "position_update", {"lat": 25.0, "lng": 121.0, "udid": PRIMARY}, None
    )
    assert msg is None


def test_unrelated_events_are_dropped():
    for evt in ("route_path", "lap_complete", "ddi_mounted", "goldditto_cycle"):
        assert follow.translate(evt, {"udid": PRIMARY, "lat": 1.0, "lng": 2.0}, PRIMARY) is None


def test_position_with_missing_or_bad_coords_is_dropped():
    assert follow.translate("position_update", {"udid": PRIMARY}, PRIMARY) is None
    assert follow.translate(
        "position_update", {"lat": "bad", "lng": 121.0, "udid": PRIMARY}, PRIMARY
    ) is None


def test_state_change_with_non_string_state_is_dropped():
    assert follow.translate("state_change", {"state": 7, "udid": PRIMARY}, PRIMARY) is None


def test_non_dict_data_is_dropped():
    assert follow.translate("position_update", None, PRIMARY) is None  # type: ignore[arg-type]


# ── hello ────────────────────────────────────────────────

def test_hello_shape():
    msg = follow.hello(PRIMARY, "0.1.0")
    assert msg == {
        "type": "hello",
        "app": "locwarp",
        "protocol": follow.PROTOCOL_VERSION,
        "version": "0.1.0",
        "udid": PRIMARY,
    }
    assert follow.hello(None, "0.1.0")["udid"] is None


# ── forward ──────────────────────────────────────────────

class FakeFollowerWs:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.sent: list[str] = []

    async def send_text(self, text: str) -> None:
        if self.fail:
            raise RuntimeError("socket dead")
        self.sent.append(text)


@pytest.fixture
def primary_engine(monkeypatch):
    """Point app_state at a primary udid without touching real hardware."""
    from main import app_state

    monkeypatch.setattr(app_state, "_primary_udid", PRIMARY)
    return app_state


@pytest.fixture(autouse=True)
def clean_followers():
    follow._followers.clear()
    yield
    follow._followers.clear()


async def test_forward_sends_to_all_followers(primary_engine):
    a, b = FakeFollowerWs(), FakeFollowerWs()
    follow._followers.extend([a, b])
    await follow.forward("position_update", {"lat": 25.0, "lng": 121.0, "udid": PRIMARY})
    assert json.loads(a.sent[0]) == {"type": "position", "lat": 25.0, "lng": 121.0}
    assert json.loads(b.sent[0]) == {"type": "position", "lat": 25.0, "lng": 121.0}


async def test_forward_removes_dead_follower_and_keeps_serving(primary_engine):
    dead, alive = FakeFollowerWs(fail=True), FakeFollowerWs()
    follow._followers.extend([dead, alive])
    await follow.forward("position_update", {"lat": 25.0, "lng": 121.0, "udid": PRIMARY})
    assert dead not in follow._followers
    assert alive in follow._followers
    assert len(alive.sent) == 1


async def test_forward_never_raises_even_if_translate_blows_up(monkeypatch, primary_engine):
    follow._followers.append(FakeFollowerWs())

    def boom(*a, **k):
        raise RuntimeError("translate bug")

    monkeypatch.setattr(follow, "translate", boom)
    await follow.forward("position_update", {"lat": 25.0, "lng": 121.0, "udid": PRIMARY})
    # reaching here without an exception IS the assertion


async def test_forward_noop_with_no_followers(primary_engine):
    await follow.forward("position_update", {"lat": 25.0, "lng": 121.0, "udid": PRIMARY})

"""Tests for the mDNS advertisement helper.

The only hard contract: start/stop NEVER raise — mDNS failure degrades to
manual-IP pairing, it must not break LocWarp startup/shutdown.
"""

from __future__ import annotations

import socket

from services import follow_discovery


def teardown_function():
    follow_discovery.stop_advertise()  # idempotent cleanup between tests


def test_service_type_matches_spec():
    assert follow_discovery.SERVICE_TYPE == "_locwarp-follow._tcp.local."


def test_local_ipv4s_returns_packed_addresses():
    addrs = follow_discovery._local_ipv4s()
    assert isinstance(addrs, list)
    for a in addrs:
        assert isinstance(a, bytes) and len(a) == 4
        assert not socket.inet_ntoa(a).startswith("127.")


def test_start_advertise_survives_zeroconf_failure(monkeypatch):
    import zeroconf

    def boom(*args, **kwargs):
        raise RuntimeError("no network stack")

    monkeypatch.setattr(zeroconf, "Zeroconf", boom)
    follow_discovery.start_advertise(8777, "0.1.0")  # must not raise
    assert follow_discovery._zc is None


def test_start_advertise_skips_when_no_local_ip(monkeypatch):
    monkeypatch.setattr(follow_discovery, "_local_ipv4s", lambda: [])
    follow_discovery.start_advertise(8777, "0.1.0")  # must not raise
    assert follow_discovery._zc is None


def test_stop_advertise_without_start_is_noop():
    follow_discovery.stop_advertise()  # must not raise


def test_start_and_stop_roundtrip(monkeypatch):
    """Register against a fake Zeroconf to verify the ServiceInfo we build."""
    import zeroconf

    registered = {}

    class FakeZeroconf:
        def register_service(self, info):
            registered["info"] = info

        def unregister_service(self, info):
            registered["unregistered"] = True

        def close(self):
            registered["closed"] = True

    monkeypatch.setattr(zeroconf, "Zeroconf", FakeZeroconf)
    monkeypatch.setattr(
        follow_discovery, "_local_ipv4s", lambda: [socket.inet_aton("192.168.1.10")]
    )
    follow_discovery.start_advertise(8777, "0.2.0")

    info = registered["info"]
    assert info.type == follow_discovery.SERVICE_TYPE
    assert info.port == 8777
    props = {k.decode(): v.decode() for k, v in info.properties.items()}
    assert props["protocol"] == "1"
    assert props["path"] == "/ws/follow"
    assert props["version"] == "0.2.0"

    follow_discovery.stop_advertise()
    assert registered.get("unregistered") is True
    assert registered.get("closed") is True
    assert follow_discovery._zc is None

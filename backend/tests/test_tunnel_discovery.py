"""Tests for services.tunnel_discovery."""

import asyncio

from services.tunnel_discovery import (
    _mdns_entries_to_candidates,
    discover_tunnel_candidates,
    find_fallback_endpoints,
)


class _FakeServiceInfo:
    """Stand-in for zeroconf.ServiceInfo (only the bits we consume)."""

    def __init__(self, name, server, port, v4, v6=()):
        self.name = name
        self.server = server
        self.port = port
        self._v4 = list(v4)
        self._v6 = list(v6)

    def parsed_scoped_addresses(self, version=None):
        from zeroconf import IPVersion
        if version is IPVersion.V4Only:
            return self._v4
        if version is IPVersion.V6Only:
            return self._v6
        return self._v4 + self._v6


def test_mdns_entries_prefer_ipv4_and_carry_port() -> None:
    infos = [
        _FakeServiceInfo(
            "AAA._remotepairing._tcp.local.", "iPhone-A.local.", 60637,
            v4=["192.168.1.109"], v6=["fe80::1"],
        ),
    ]
    assert _mdns_entries_to_candidates(infos) == [
        {"ip": "192.168.1.109", "port": 60637, "host": "iPhone-A.local",
         "name": "AAA._remotepairing._tcp.local.", "method": "mdns"},
    ]


def test_mdns_entries_fall_back_to_ipv6_when_no_ipv4() -> None:
    infos = [
        _FakeServiceInfo(
            "BBB._remotepairing._tcp.local.", "iPhone-B.local.", 50877,
            v4=[], v6=["fd92:378c:9b00::1"],
        ),
    ]
    assert [c["ip"] for c in _mdns_entries_to_candidates(infos)] == ["fd92:378c:9b00::1"]


def test_mdns_entries_skip_entries_without_port_or_address() -> None:
    infos = [
        _FakeServiceInfo("C._remotepairing._tcp.local.", "c.local.", None, v4=["192.168.1.5"]),
        _FakeServiceInfo("D._remotepairing._tcp.local.", "d.local.", 50000, v4=[]),
    ]
    assert _mdns_entries_to_candidates(infos) == []


async def test_mdns_results_returned_and_deduped() -> None:
    async def fake_browse() -> list[dict]:
        return [
            {"ip": "192.168.1.10", "port": 50000, "host": "a", "name": "iPhone A", "method": "mdns"},
            {"ip": "192.168.1.10", "port": 50000, "host": "a", "name": "iPhone A", "method": "mdns"},
            {"ip": "192.168.1.11", "port": 50001, "host": "b", "name": "iPhone B", "method": "mdns"},
        ]

    async def fake_subnet_scan(port: int) -> list[str]:
        return []

    result = await discover_tunnel_candidates(
        browse=fake_browse, subnet_scan=fake_subnet_scan,
    )
    assert [(r["ip"], r["port"]) for r in result] == [
        ("192.168.1.10", 50000),
        ("192.168.1.11", 50001),
    ]


async def test_partial_mdns_results_are_supplemented_by_subnet_scan() -> None:
    async def fake_browse() -> list[dict]:
        return [
            {"ip": "192.168.1.108", "port": 51609, "host": "a",
             "name": "iPhone A", "method": "mdns"},
        ]

    subnet_calls: list[int] = []

    async def fake_subnet_scan(port: int) -> list[str]:
        subnet_calls.append(port)
        if port == 49152:
            return ["192.168.1.116"]
        return ["192.168.1.102", "192.168.1.116"]

    port_scan_calls: list[str] = []

    async def fake_port_scan(ip: str) -> list[int]:
        port_scan_calls.append(ip)
        return {"192.168.1.102": [], "192.168.1.116": [53515]}[ip]

    result = await discover_tunnel_candidates(
        browse=fake_browse,
        subnet_scan=fake_subnet_scan,
        port_scan=fake_port_scan,
    )

    assert [(r["ip"], r["port"]) for r in result] == [
        ("192.168.1.108", 51609),
        ("192.168.1.116", 53515),
    ]
    assert subnet_calls == [49152, 62078]
    assert set(port_scan_calls) == {"192.168.1.102", "192.168.1.116"}
    assert "192.168.1.108" not in port_scan_calls


async def test_multiple_addresses_for_one_mdns_service_still_trigger_supplement() -> None:
    async def fake_browse() -> list[dict]:
        return [
            {"ip": f"192.168.1.10{i}", "port": 50000 + i,
             "host": "iPhone-A.local", "name": "iPhone A service", "method": "mdns"}
            for i in range(8, 11)
        ]

    subnet_calls: list[int] = []

    async def fake_subnet_scan(port: int) -> list[str]:
        subnet_calls.append(port)
        return ["192.168.1.116"] if port == 49152 else []

    async def fake_port_scan(ip: str) -> list[int]:
        assert ip == "192.168.1.116"
        return [53515]

    result = await discover_tunnel_candidates(
        browse=fake_browse,
        subnet_scan=fake_subnet_scan,
        port_scan=fake_port_scan,
    )

    assert subnet_calls == [49152, 62078]
    assert ("192.168.1.116", 53515) in {
        (candidate["ip"], candidate["port"]) for candidate in result
    }


async def test_partial_mdns_scan_deadline_cancels_scan_and_keeps_mdns_result(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "services.tunnel_discovery._PARTIAL_DISCOVERY_SCAN_TIMEOUT", 0.01,
    )

    mdns_candidate = {
        "ip": "192.168.1.108", "port": 51609, "host": "a",
        "name": "iPhone A", "method": "mdns",
    }
    scan_started = asyncio.Event()
    scan_cancelled = asyncio.Event()

    async def fake_browse() -> list[dict]:
        return [mdns_candidate]

    async def fake_subnet_scan(_port: int) -> list[str]:
        return ["192.168.1.116"]

    async def blocking_port_scan(_ip: str) -> list[int]:
        scan_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            scan_cancelled.set()
            raise

    result = await discover_tunnel_candidates(
        browse=fake_browse,
        subnet_scan=fake_subnet_scan,
        port_scan=blocking_port_scan,
    )

    assert result == [mdns_candidate]
    assert scan_started.is_set()
    assert scan_cancelled.is_set()


async def test_empty_mdns_fallback_is_not_limited_by_partial_deadline(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "services.tunnel_discovery._PARTIAL_DISCOVERY_SCAN_TIMEOUT", 0.01,
    )

    async def fake_browse() -> list[dict]:
        return []

    async def fake_subnet_scan(_port: int) -> list[str]:
        return ["192.168.1.116"]

    async def delayed_port_scan(_ip: str) -> list[int]:
        await asyncio.sleep(0.03)
        return [53515]

    result = await discover_tunnel_candidates(
        browse=fake_browse,
        subnet_scan=fake_subnet_scan,
        port_scan=delayed_port_scan,
    )

    assert result == [
        {"ip": "192.168.1.116", "port": 53515,
         "host": "192.168.1.116", "name": "192.168.1.116",
         "method": "tcp_scan"},
    ]


async def test_complete_mdns_results_skip_subnet_and_port_scans() -> None:
    async def fake_browse() -> list[dict]:
        return [
            {"ip": f"192.168.1.10{i}", "port": 50000 + i,
             "host": f"iPhone-{i}", "name": f"iPhone {i}", "method": "mdns"}
            for i in range(1, 4)
        ]

    subnet_calls: list[int] = []
    port_scan_calls: list[str] = []

    async def fake_subnet_scan(port: int) -> list[str]:
        subnet_calls.append(port)
        return []

    async def fake_port_scan(ip: str) -> list[int]:
        port_scan_calls.append(ip)
        return []

    result = await discover_tunnel_candidates(
        browse=fake_browse,
        subnet_scan=fake_subnet_scan,
        port_scan=fake_port_scan,
    )

    assert len(result) == 3
    assert subnet_calls == []
    assert port_scan_calls == []


async def test_mdns_empty_falls_back_to_subnet_scan() -> None:
    async def fake_browse() -> list[dict]:
        return []

    async def fake_subnet_scan(port: int) -> list[str]:
        return ["192.168.1.20"] if port == 49152 else []

    async def fake_port_scan(ip: str) -> list[int]:
        assert ip == "192.168.1.20"
        return [51234, 62078]

    result = await discover_tunnel_candidates(
        browse=fake_browse, subnet_scan=fake_subnet_scan, port_scan=fake_port_scan,
    )
    # Every valid RemotePairing port must survive as its own candidate.
    # lockdownd's well-known 62078 listener is also visible during scans but
    # is not a RemotePairing endpoint and must be filtered centrally.
    assert result == [
        {"ip": "192.168.1.20", "port": 51234, "host": "192.168.1.20",
         "name": "192.168.1.20", "method": "tcp_scan"},
    ]


async def test_browse_exception_still_falls_back() -> None:
    async def bad_browse() -> list[dict]:
        raise RuntimeError("mdns broken")

    async def fake_subnet_scan(port: int) -> list[str]:
        return []

    result = await discover_tunnel_candidates(
        browse=bad_browse, subnet_scan=fake_subnet_scan,
    )
    assert result == []


async def test_fallback_prefers_same_ip_ports_then_discover() -> None:
    async def fake_port_scan(ip: str) -> list[int]:
        assert ip == "192.168.1.109"
        return [50100, 50200]

    async def fake_discover() -> list[dict]:
        return [
            {"ip": "192.168.1.109", "port": 50100},  # duplicate of port-scan hit
            {"ip": "192.168.1.50", "port": 51000},
        ]

    result = await find_fallback_endpoints(
        "192.168.1.109", port_scan=fake_port_scan, discover=fake_discover,
    )
    assert result == [
        ("192.168.1.109", 50100),
        ("192.168.1.109", 50200),
        ("192.168.1.50", 51000),
    ]


async def test_fallback_without_ip_uses_discover_only() -> None:
    async def fake_discover() -> list[dict]:
        return [{"ip": "192.168.1.50", "port": 51000}]

    called = False

    async def fake_port_scan(ip: str) -> list[int]:
        nonlocal called
        called = True
        return []

    result = await find_fallback_endpoints(
        None, port_scan=fake_port_scan, discover=fake_discover,
    )
    assert result == [("192.168.1.50", 51000)]
    assert called is False


async def test_fallback_tolerates_phase_failures() -> None:
    async def bad_port_scan(ip: str) -> list[int]:
        raise OSError("scan blew up")

    async def bad_discover() -> list[dict]:
        raise RuntimeError("discover blew up")

    result = await find_fallback_endpoints(
        "192.168.1.109", port_scan=bad_port_scan, discover=bad_discover,
    )
    assert result == []

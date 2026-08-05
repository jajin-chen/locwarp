"""Tests for services.tunnel_discovery."""

from services.tunnel_discovery import discover_tunnel_candidates, find_fallback_endpoints


async def test_mdns_results_returned_and_deduped() -> None:
    async def fake_browse() -> list[dict]:
        return [
            {"ip": "192.168.1.10", "port": 50000, "host": "a", "name": "iPhone A", "method": "mdns"},
            {"ip": "192.168.1.10", "port": 50000, "host": "a", "name": "iPhone A", "method": "mdns"},
            {"ip": "192.168.1.11", "port": 50001, "host": "b", "name": "iPhone B", "method": "mdns"},
        ]

    result = await discover_tunnel_candidates(browse=fake_browse)
    assert [(r["ip"], r["port"]) for r in result] == [
        ("192.168.1.10", 50000),
        ("192.168.1.11", 50001),
    ]


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

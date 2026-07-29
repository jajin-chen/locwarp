"""Tests for services.tunnel_discovery."""

from services.tunnel_discovery import discover_tunnel_candidates


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

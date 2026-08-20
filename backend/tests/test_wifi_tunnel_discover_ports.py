"""Regression coverage for the discover endpoint's port-hint shape."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from api import device


async def test_discover_aggregates_ports_per_ip_and_keeps_first_as_primary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_discover() -> list[dict]:
        return [
            {
                "ip": "192.0.2.10",
                "port": 51234,
                "host": "iphone.local",
                "name": "iPhone",
                "method": "tcp_scan",
            },
            {
                "ip": "192.0.2.10",
                "port": 52000,
                "host": "iphone.local",
                "name": "iPhone",
                "method": "tcp_scan",
            },
            {
                "ip": "192.0.2.10",
                "port": 62078,
                "host": "iphone.local",
                "name": "iPhone",
                "method": "tcp_scan",
            },
            {
                "ip": "192.0.2.11",
                "port": 53000,
                "host": "second.local",
                "name": "Second",
                "method": "mdns",
            },
        ]

    monkeypatch.setattr(device, "discover_tunnel_candidates", fake_discover)

    result = await device.wifi_tunnel_discover()

    assert result == {
        "devices": [
            {
                "ip": "192.0.2.10",
                "port": 51234,
                "ports": [51234, 52000],
                "host": "iphone.local",
                "name": "iPhone",
                "method": "tcp_scan",
            },
            {
                "ip": "192.0.2.11",
                "port": 53000,
                "ports": [53000],
                "host": "second.local",
                "name": "Second",
                "method": "mdns",
            },
        ],
    }


async def test_find_port_preserves_scan_exception_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scanner failure keeps its 500 mapping instead of raising NameError."""

    async def fail_scan(_ip: str) -> list[int]:
        raise RuntimeError("probe backend unavailable")

    monkeypatch.setattr(device, "_scan_ports_for_ip", fail_scan)

    with pytest.raises(HTTPException) as caught:
        await device.wifi_tunnel_find_port(
            device.WifiTunnelFindPortRequest(ip="192.0.2.10"),
        )

    assert caught.value.status_code == 500
    assert caught.value.detail == "probe backend unavailable"

"""WiFi tunnel candidate discovery (mDNS + subnet/port scanning).

Extracted from api/device.py so the /wifi/tunnel/discover endpoint and
the tunnel watchdog's reconnect fallback share one implementation.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("wifi_tunnel")


def _get_primary_local_ip() -> str | None:
    """Return this machine's primary IPv4 (the one used to reach the internet)."""
    import socket as _s
    try:
        s = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return None


async def _tcp_probe(ip: str, port: int, timeout: float = 0.4) -> bool:
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, port), timeout=timeout,
        )
        writer.close()
        try:
            await writer.wait_closed()
        except (OSError, ConnectionError):
            pass
        return True
    except (OSError, ConnectionError, asyncio.TimeoutError):
        return False


async def _scan_subnet_for_port(port: int = 49152) -> list[str]:
    """Scan the local /24 subnet for hosts responding on the given TCP port."""
    my_ip = _get_primary_local_ip()
    if not my_ip:
        return []
    try:
        parts = my_ip.split(".")
        prefix = ".".join(parts[:3])
    except (AttributeError, IndexError):
        return []

    candidates = [f"{prefix}.{i}" for i in range(1, 255) if f"{prefix}.{i}" != my_ip]
    results = await asyncio.gather(
        *[_tcp_probe(ip, port, 0.4) for ip in candidates],
        return_exceptions=True,
    )
    hits = [ip for ip, ok in zip(candidates, results) if ok is True]
    return hits


async def _scan_ports_for_ip(
    ip: str,
    start: int = 49152,
    end: int = 65535,
    concurrency: int = 1024,
    timeout: float = 0.35,
) -> list[int]:
    """Scan the IANA dynamic / ephemeral range on a single IP for open TCP ports.

    iOS picks the RemotePairing port from this range at boot / network rebind,
    so the actual port on a given iPhone is rarely the legacy 49152 default.
    Scanning one host across 16k ports finishes in a few seconds because most
    closed ports return RST immediately on a same-LAN probe.
    """
    sem = asyncio.Semaphore(concurrency)

    async def _probe_one(p: int) -> int | None:
        async with sem:
            ok = await _tcp_probe(ip, p, timeout)
            return p if ok else None

    tasks = [asyncio.create_task(_probe_one(p)) for p in range(start, end + 1)]
    hits: list[int] = []
    for fut in asyncio.as_completed(tasks):
        try:
            res = await fut
        except (OSError, ConnectionError, asyncio.TimeoutError):
            res = None
        if res is not None:
            hits.append(res)
    hits.sort()
    return hits


async def _browse_mdns() -> list[dict]:
    """mDNS / Bonjour RemotePairing broadcast → candidate dicts."""
    from pymobiledevice3.bonjour import browse_remotepairing

    results: list[dict] = []
    instances = await browse_remotepairing(timeout=3.0)
    for inst in instances:
        raw_addrs = inst.addresses or []
        str_addrs: list[str] = []
        for a in raw_addrs:
            if hasattr(a, "ip"):
                str_addrs.append(str(a.ip))
            else:
                str_addrs.append(str(a))
        ipv4s = [s for s in str_addrs if ":" not in s]
        addrs = ipv4s if ipv4s else str_addrs
        for addr in addrs:
            results.append({
                "ip": addr,
                "port": inst.port,
                "host": inst.host,
                "name": inst.instance or inst.host,
                "method": "mdns",
            })
    return results


async def discover_tunnel_candidates(
    *,
    browse=None,
    subnet_scan=None,
    port_scan=None,
) -> list[dict]:
    """Find iPhones on the local network. First tries mDNS; if that yields
    nothing, falls back to the smart /24 scan (probe 49152 + 62078, then
    full-range port scan per live host). Deduped on (ip, port).

    The browse / subnet_scan / port_scan hooks exist for tests only.
    """
    browse = browse or _browse_mdns
    subnet_scan = subnet_scan or _scan_subnet_for_port
    port_scan = port_scan or _scan_ports_for_ip
    results: list[dict] = []

    try:
        results.extend(await browse())
    except Exception as e:
        logger.warning("mDNS browse failed: %s", e)

    if not results:
        logger.info("mDNS empty; falling back to smart /24 scan (probe + full-range)")
        try:
            candidates: set[str] = set()
            for p in (49152, 62078):
                try:
                    candidates.update(await subnet_scan(p))
                except Exception as e:
                    logger.warning("probe scan port %d failed: %s", p, e)

            if candidates:
                logger.info(
                    "Smart scan found %d live host(s); full-range scanning each",
                    len(candidates),
                )

                async def _scan_one(ip: str) -> tuple[str, list[int]]:
                    try:
                        return ip, await port_scan(ip)
                    except Exception as e:
                        logger.warning("port scan for %s failed: %s", ip, e)
                        return ip, []

                scan_results = await asyncio.gather(*[_scan_one(ip) for ip in candidates])
                for ip, ports in scan_results:
                    if not ports:
                        continue
                    results.append({
                        "ip": ip, "port": ports[0], "host": ip,
                        "name": ip, "method": "tcp_scan",
                    })
        except Exception as e:
            logger.warning("Smart fallback scan failed: %s", e)

    seen: set[tuple] = set()
    unique: list[dict] = []
    for r in results:
        key = (r["ip"], r["port"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(r)
    return unique


async def find_fallback_endpoints(
    ip: str | None,
    *,
    port_scan=None,
    discover=None,
) -> list[tuple[str, int]]:
    """Ordered candidate endpoints to try after direct reconnects failed.

    1. Every open dynamic-range port on the last-known IP — cheap (a few
       seconds), covers the common case where the iPhone rebound its
       RemotePairing port after a reboot / WiFi rejoin.
    2. Full discover results — covers a DHCP address change.

    Deduped on (ip, port); each phase tolerates failure independently.
    """
    port_scan = port_scan or _scan_ports_for_ip
    discover = discover or discover_tunnel_candidates
    out: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()

    if ip:
        try:
            for p in await port_scan(ip):
                key = (ip, int(p))
                if key not in seen:
                    seen.add(key)
                    out.append(key)
        except Exception:
            logger.warning("Fallback port scan failed for %s", ip, exc_info=True)

    try:
        for cand in await discover():
            key = (str(cand["ip"]), int(cand["port"]))
            if key not in seen:
                seen.add(key)
                out.append(key)
    except Exception:
        logger.warning("Fallback discover failed", exc_info=True)

    return out

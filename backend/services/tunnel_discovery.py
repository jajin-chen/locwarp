"""WiFi tunnel candidate discovery (mDNS + subnet/port scanning).

Extracted from api/device.py so the /wifi/tunnel/discover endpoint and
the tunnel watchdog's reconnect fallback share one implementation.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import weakref
from collections.abc import Iterable

logger = logging.getLogger("wifi_tunnel")


# Windows' SelectorEventLoop delegates to ``select.select`` and cannot watch
# more than 512 sockets.  A full 16k-port scan used to open 1024 sockets at a
# time, which could exhaust that process-wide budget while three DTX tunnels
# and the HTTP server were still active.  Keep discovery probes in a shared,
# per-event-loop budget so concurrent watchdog/API scans cannot multiply the
# limit.  The value leaves headroom for the live tunnel and control-plane
# sockets instead of treating the select limit as a target.
_SCAN_CONCURRENCY = 96
_probe_budgets: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, asyncio.Semaphore
] = weakref.WeakKeyDictionary()
_probe_budgets_lock = threading.Lock()


def _probe_budget() -> asyncio.Semaphore:
    """Return the scanner budget for the currently running event loop."""
    loop = asyncio.get_running_loop()
    with _probe_budgets_lock:
        budget = _probe_budgets.get(loop)
        if budget is None:
            budget = asyncio.Semaphore(_SCAN_CONCURRENCY)
            _probe_budgets[loop] = budget
        return budget


# TCP scanning can see lockdownd in the same dynamic range as RemotePairing.
# Keep this in the shared discovery module so API discovery, manual scans, and
# watchdog fallback all apply the same safety filter.
NON_REMOTEPAIRING_PORTS = frozenset({62078})


def filter_remotepairing_ports(ports: Iterable[int]) -> list[int]:
    """Return valid port values in input order, excluding known wrong ports."""
    filtered: list[int] = []
    for raw in ports:
        try:
            port = int(raw)
        except (TypeError, ValueError):
            continue
        if port in NON_REMOTEPAIRING_PORTS or port in filtered:
            continue
        filtered.append(port)
    return filtered


# Keep a private spelling available to existing internal callers/tests while
# exposing the public helper for API and watchdog consumers.
_filter_remotepairing_ports = filter_remotepairing_ports


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
    budget = _probe_budget()

    async def _probe_host(ip: str) -> bool:
        async with budget:
            return await _tcp_probe(ip, port, 0.4)

    results = await asyncio.gather(
        *[_probe_host(ip) for ip in candidates],
        return_exceptions=True,
    )
    hits = [ip for ip, ok in zip(candidates, results) if ok is True]
    return hits


async def _scan_ports_for_ip(
    ip: str,
    start: int = 49152,
    end: int = 65535,
    concurrency: int = _SCAN_CONCURRENCY,
    timeout: float = 0.35,
) -> list[int]:
    """Scan the IANA dynamic / ephemeral range on a single IP for open TCP ports.

    iOS picks the RemotePairing port from this range at boot / network rebind,
    so the actual port on a given iPhone is rarely the legacy 49152 default.
    Scanning one host across 16k ports finishes in a few seconds because most
    closed ports return RST immediately on a same-LAN probe.
    """
    # ``concurrency`` remains injectable for focused tests, but never permits
    # a caller to bypass the process-wide selector safety budget.  The shared
    # semaphore also covers multiple watchdog scans running at once.
    sem = asyncio.Semaphore(max(1, min(concurrency, _SCAN_CONCURRENCY)))
    budget = _probe_budget()

    async def _probe_one(p: int) -> int | None:
        async with sem:
            async with budget:
                ok = await _tcp_probe(ip, p, timeout)
            return p if ok else None

    ports = list(range(start, end + 1))
    queue: asyncio.Queue[int] = asyncio.Queue()
    for port in ports:
        queue.put_nowait(port)

    hits: list[int] = []
    worker_count = min(len(ports), max(1, min(concurrency, _SCAN_CONCURRENCY)))

    async def _worker() -> None:
        while True:
            try:
                port = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                try:
                    result = await _probe_one(port)
                except (OSError, ConnectionError, asyncio.TimeoutError):
                    result = None
                if result is not None:
                    hits.append(result)
            finally:
                queue.task_done()

    workers = [asyncio.create_task(_worker()) for _ in range(worker_count)]
    try:
        await asyncio.gather(*workers)
    except BaseException:
        # A start budget timeout or caller cancellation must stop and drain
        # the bounded worker set before propagating.  Unlike the old
        # 16,384-task implementation, no large pending task fan-out remains.
        for worker in workers:
            if not worker.done():
                worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        raise
    return filter_remotepairing_ports(sorted(hits))


REMOTEPAIRING_SERVICE = "_remotepairing._tcp.local."
_MDNS_BROWSE_SECONDS = 3.0


def _mdns_entries_to_candidates(infos) -> list[dict]:
    """zeroconf ServiceInfo objects → candidate dicts (IPv4 preferred)."""
    from zeroconf import IPVersion

    results: list[dict] = []
    for info in infos:
        port = getattr(info, "port", None)
        if not port:
            continue
        if int(port) in NON_REMOTEPAIRING_PORTS:
            continue
        addrs = info.parsed_scoped_addresses(version=IPVersion.V4Only)
        if not addrs:
            addrs = info.parsed_scoped_addresses(version=IPVersion.V6Only)
        if not addrs:
            continue
        server = getattr(info, "server", None) or ""
        host = server[:-1] if server.endswith(".") else server
        for addr in addrs:
            results.append({
                "ip": addr,
                "port": int(port),
                "host": host,
                "name": getattr(info, "name", None) or host,
                "method": "mdns",
            })
    return results


async def _browse_mdns() -> list[dict]:
    """mDNS / Bonjour RemotePairing broadcast → candidate dicts.

    Uses zeroconf rather than pymobiledevice3's bonjour helper. Since
    pymobiledevice3 10.2 that helper is a hand-rolled raw-socket
    implementation whose IPv4 multicast join passes INADDR_ANY; on Windows
    that binds the group to a single OS-chosen interface, which loses to a
    virtual adapter (Docker/WSL/Hyper-V) on multi-NIC machines. It then
    silently returns zero instances forever and every discovery cycle pays
    the ~11s subnet-scan fallback instead. zeroconf joins the group on every
    interface explicitly, so it sees the phones. zeroconf is already a
    dependency (services/follow_discovery.py advertises through it).
    """
    from zeroconf import ServiceStateChange
    from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

    found: dict[str, None] = {}

    def _on_change(zeroconf, service_type, name, state_change) -> None:
        if state_change is ServiceStateChange.Added:
            found[name] = None

    azc = AsyncZeroconf()
    try:
        browser = AsyncServiceBrowser(
            azc.zeroconf, REMOTEPAIRING_SERVICE, handlers=[_on_change],
        )
        try:
            await asyncio.sleep(_MDNS_BROWSE_SECONDS)
        finally:
            await browser.async_cancel()

        infos = []
        for name in found:
            info = AsyncServiceInfo(REMOTEPAIRING_SERVICE, name)
            if await info.async_request(azc.zeroconf, 2000):
                infos.append(info)
    finally:
        await azc.async_close()

    return _mdns_entries_to_candidates(infos)


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
                    # Keep EVERY open port. RemotePairing binds one port from
                    # the dynamic range and iOS usually has other high ports
                    # open, so the lowest hit is often wrong — dropping the
                    # rest cost a 10s tunnel timeout per miss.
                    for p in filter_remotepairing_ports(ports):
                        results.append({
                            "ip": ip, "port": p, "host": ip,
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
            for p in filter_remotepairing_ports(await port_scan(ip)):
                key = (ip, int(p))
                if key not in seen:
                    seen.add(key)
                    out.append(key)
        except Exception:
            logger.warning("Fallback port scan failed for %s", ip, exc_info=True)

    try:
        for cand in await discover():
            ip_value = str(cand["ip"])
            hinted = [cand.get("port"), *(cand.get("ports") or [])]
            for p in filter_remotepairing_ports(hinted):
                key = (ip_value, int(p))
                if key not in seen:
                    seen.add(key)
                    out.append(key)
    except Exception:
        logger.warning("Fallback discover failed", exc_info=True)

    return out

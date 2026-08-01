"""mDNS advertisement for the /ws/follow follower feed.

Registers ``_locwarp-follow._tcp.local.`` so VirtualRun (Android
NsdManager) can auto-discover this machine on the LAN. Everything here is
best-effort: registration failure degrades to manual-IP pairing and must
never break LocWarp startup or shutdown.
"""

from __future__ import annotations

import logging
import re
import socket

logger = logging.getLogger(__name__)

SERVICE_TYPE = "_locwarp-follow._tcp.local."

_zc = None
_info = None


def _local_ipv4s() -> list[bytes]:
    """All non-loopback IPv4 addresses of this machine, packed for zeroconf."""
    addrs: set[str] = set()
    try:
        for res in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = res[4][0]
            if not ip.startswith("127."):
                addrs.add(ip)
    except OSError:
        pass
    if not addrs:
        # Fallback: outbound-route trick — no packet is actually sent.
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect(("8.8.8.8", 80))
                addrs.add(s.getsockname()[0])
            finally:
                s.close()
        except OSError:
            pass
    return [socket.inet_aton(a) for a in sorted(addrs)]


def start_advertise(port: int, version: str) -> None:
    """Register the mDNS service. Never raises; failure logs a warning."""
    global _zc, _info
    try:
        from zeroconf import ServiceInfo, Zeroconf

        addresses = _local_ipv4s()
        if not addresses:
            logger.warning("follow mDNS: no local IPv4 found; manual-IP pairing only")
            return
        host = re.sub(r"[^A-Za-z0-9-]", "-", socket.gethostname()) or "PC"
        _info = ServiceInfo(
            SERVICE_TYPE,
            f"LocWarp-{host}.{SERVICE_TYPE}",
            addresses=addresses,
            port=port,
            properties={"protocol": "1", "version": version, "path": "/ws/follow"},
        )
        _zc = Zeroconf()
        _zc.register_service(_info)
        logger.info("follow mDNS registered (%s, port %d)", host, port)
    except Exception:
        _zc = None
        _info = None
        logger.warning(
            "follow mDNS registration failed; manual-IP pairing only", exc_info=True
        )


def stop_advertise() -> None:
    """Unregister and close. Never raises; idempotent."""
    global _zc, _info
    try:
        if _zc is not None:
            if _info is not None:
                _zc.unregister_service(_info)
            _zc.close()
    except Exception:
        logger.debug("follow mDNS unregister failed (ignored)", exc_info=True)
    finally:
        _zc = None
        _info = None

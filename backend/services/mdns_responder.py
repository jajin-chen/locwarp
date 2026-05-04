"""mDNS / Bonjour responder for the phone-control web UI.

Broadcasts `locwarp.local` so users on the same LAN can reach the desktop
without knowing its IP. iPhone Safari and Android 12+ Chrome resolve
`.local` natively via Bonjour / system mDNS resolver.

We register two things:

  * An A record for `locwarp.local` pointing at every reachable IPv4 of
    this machine. iOS prefers the first reachable address it sees, so we
    list the user's WiFi/Ethernet IP — virtual NICs (Hyper-V, WSL, VPN)
    are filtered upstream before being passed in.
  * A `_http._tcp.local.` service entry so other Bonjour browsers (e.g.
    "Bonjour Browser" tools) can discover LocWarp.

Limits: still goes through Windows Firewall, AP isolation still kills it,
and only one LocWarp per LAN can claim the name. These are documented in
the phone-control UI hints.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from typing import Iterable

logger = logging.getLogger("locwarp.mdns")


_HOSTNAME = "locwarp"  # → locwarp.local


class MdnsResponder:
    """Wraps a zeroconf instance + registered ServiceInfo so we can stop
    cleanly on shutdown. zeroconf runs its own IO thread, so this class
    is safe to call from sync code (e.g. FastAPI lifespan)."""

    def __init__(self) -> None:
        self._zc = None
        self._info = None
        self._registered_ips: list[str] = []
        self._last_error: str | None = None

    def start(self, ips: Iterable[str], port: int) -> list[str]:
        """Register `locwarp.local` for the given IPv4 list. Returns the
        list of IPs that were actually broadcast (filtered to valid
        IPv4). Idempotent: calling twice rebinds with the new IPs."""
        if self._zc is not None:
            self.stop()

        self._last_error = None

        valid_ips = [
            ip for ip in ips
            if ip and _is_valid_ipv4(ip) and not ip.startswith("127.")
        ]
        if not valid_ips:
            self._last_error = "no_valid_ipv4"
            logger.warning("mDNS: no valid IPv4 to broadcast; skipping registration")
            return []

        try:
            from zeroconf import IPVersion, ServiceInfo, Zeroconf
        except ImportError as e:
            self._last_error = f"zeroconf_import_failed: {e}"
            logger.warning("mDNS: zeroconf not installed; .local URL unavailable (%s)", e)
            return []

        try:
            zc = Zeroconf(ip_version=IPVersion.V4Only)
        except Exception as e:
            self._last_error = f"zeroconf_init_failed: {type(e).__name__}: {e}"
            logger.exception("mDNS: failed to start Zeroconf")
            return []

        addresses = [socket.inet_aton(ip) for ip in valid_ips]
        info = ServiceInfo(
            type_="_http._tcp.local.",
            name="LocWarp._http._tcp.local.",
            addresses=addresses,
            port=port,
            properties={"path": "/phone"},
            server=f"{_HOSTNAME}.local.",
        )

        # allow_name_change=True: if another LocWarp instance on the LAN
        # already claimed `LocWarp._http._tcp.local.`, zeroconf appends a
        # suffix. Hostname (`locwarp.local.`) is a separate field — it
        # stays universal. This avoids a hard-fail collision blowing the
        # whole .local URL away when, e.g., the user has two PCs running.
        try:
            zc.register_service(info, allow_name_change=True)
        except Exception as e:
            self._last_error = f"register_failed: {type(e).__name__}: {e}"
            logger.exception("mDNS: failed to register service")
            try:
                zc.close()
            except Exception:
                pass
            return []

        self._zc = zc
        self._info = info
        self._registered_ips = valid_ips
        logger.info(
            "mDNS: registered %s.local on port %d (ips=%s)",
            _HOSTNAME, port, valid_ips,
        )
        return valid_ips

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def stop(self) -> None:
        if self._zc is None:
            return
        try:
            if self._info is not None:
                try:
                    self._zc.unregister_service(self._info)
                except Exception:
                    logger.debug("mDNS: unregister_service failed", exc_info=True)
            self._zc.close()
        except Exception:
            logger.debug("mDNS: close failed", exc_info=True)
        finally:
            self._zc = None
            self._info = None
            self._registered_ips = []
            logger.info("mDNS: stopped")

    @property
    def hostname(self) -> str:
        return f"{_HOSTNAME}.local"

    @property
    def registered_ips(self) -> list[str]:
        return list(self._registered_ips)


def _is_valid_ipv4(ip: str) -> bool:
    try:
        ipaddress.IPv4Address(ip)
        return True
    except (ipaddress.AddressValueError, ValueError):
        return False


_singleton: MdnsResponder | None = None


def get_responder() -> MdnsResponder:
    global _singleton
    if _singleton is None:
        _singleton = MdnsResponder()
    return _singleton

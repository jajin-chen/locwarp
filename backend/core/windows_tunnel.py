"""Keep synchronous Windows adapter setup off the backend event loop."""

import asyncio
import logging
import sys

logger = logging.getLogger(__name__)


class HostTunnelError(RuntimeError):
    code = "host_tunnel_unavailable"

    def __init__(self, cause: Exception):
        self.winerror = getattr(cause, "winerror", None) or getattr(cause, "errno", None)
        super().__init__(f"Windows Wintun 網路介面建立失敗 ({self.winerror}): {cause}")


def _create_adapter(factory, interface_name, address, mtu):
    adapter = None
    try:
        adapter = factory(interface_name)
        adapter.addr = address
        adapter.mtu = mtu
        adapter.up()
        return adapter
    except Exception as exc:
        if adapter is not None:
            try:
                adapter.close()
            except Exception:
                logger.exception("Failed to close partially configured Wintun adapter")
        raise HostTunnelError(exc) from exc


async def _start_windows_tunnel(client, module, address, mtu, interface_name):
    # Shield the thread's future: cancelling asyncio cannot stop a native
    # Wintun call. Retain ownership until it finishes, then close late success.
    creation = asyncio.create_task(asyncio.to_thread(
        _create_adapter, module.TunTapDevice, interface_name, address, mtu,
    ))
    cancelled = False
    while True:
        try:
            adapter = await asyncio.shield(creation)
            break
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        async def cleanup():
            try:
                await asyncio.to_thread(adapter.close)
            finally:
                # The upstream TCP context only catches Exception before its
                # first yield, so CancelledError must close its TLS socket here.
                await client.stop_tunnel()

        closing = asyncio.create_task(cleanup())
        while True:
            try:
                await asyncio.shield(closing)
                break
            except asyncio.CancelledError:
                continue
        raise asyncio.CancelledError
    client.tun = adapter
    client._tun_read_task = asyncio.create_task(
        client.tun_read_task(), name=f"tun-read-{address}",
    )


def install_windows_tunnel_hook(module):
    """Install once; delegate unchanged for non-Windows/userspace tunnels."""
    if sys.platform != "win32":
        return
    cls = module.RemotePairingTunnel
    original = cls.start_tunnel
    if getattr(original, "_locwarp_windows_adapter", False):
        return

    async def start_tunnel(client, address, mtu, interface_name=module.DEFAULT_INTERFACE_NAME):
        if module.USE_USERSPACE_TUNNEL:
            return await original(client, address, mtu, interface_name)
        return await _start_windows_tunnel(client, module, address, mtu, interface_name)

    start_tunnel._locwarp_windows_adapter = True
    cls.start_tunnel = start_tunnel

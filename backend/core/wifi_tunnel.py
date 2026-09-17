"""Own WiFi tunnel lifetime and per-device RSD readiness.

Python 3.13 provides native TLS-PSK support. The process transport isolates
each userspace stack; legacy kernel transport runs in the backend loop.
The tunnel context (`service.start_tcp_tunnel()`) must stay open for the
RSD link to remain usable, so we hold it inside a long-running task and
release it via a stop event.
"""

import asyncio
import logging
import os

from core.windows_tunnel import HostTunnelError, install_windows_tunnel_hook

logger = logging.getLogger("wifi_tunnel")


# Closing a RemotePairing control service is part of runner ownership.  Keep
# the wait bounded so a wedged DTX close cannot hold the runner lock forever;
# the short drain also gives a cancellation-aware implementation a chance to
# finish and lets us retrieve its result before the task reference is gone.
_REMOTE_PAIRING_CLOSE_TIMEOUT = 5.0
_REMOTE_PAIRING_CLOSE_DRAIN_TIMEOUT = 1.0
_USERSPACE_TUNNEL_ACTIVE = False


def _userspace_tunnel_requested() -> bool:
    """Return whether WiFi tunnels should use the in-process userspace stack."""
    return os.getenv("LOCWARP_USE_USERSPACE_TUNNEL", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


class TunnelRunner:
    """Owns the tunnel asyncio task and its RSD info."""

    def __init__(self) -> None:
        self.info: dict | None = None
        self.task: asyncio.Task | None = None
        self.lock = asyncio.Lock()
        self._stop: asyncio.Event = asyncio.Event()
        self._ready: asyncio.Event = asyncio.Event()
        self._error: BaseException | None = None
        # In userspace mode the runner owns an already-connected RSD because
        # its address is reachable only through UserspaceDialPlane, not via a
        # host routing-table entry. Kernel mode leaves this None and the
        # DeviceManager creates the RSD over the WinTun interface.
        self.rsd = None
        self._dial_plane = None
        # Original (ip, port) the runner was launched against. Useful so
        # callers can tell "is the running tunnel actually for the same
        # iPhone the user is trying to connect to right now?" without
        # having to re-resolve the udid.
        self.target_ip: str | None = None
        self.target_port: int | None = None

    def is_running(self) -> bool:
        return self.task is not None and not self.task.done()

    async def _close_remote_pairing_service(self, service) -> None:
        """Close the control socket without blocking runner teardown forever."""
        close_task = asyncio.create_task(service.close())
        caller_cancelled = False
        try:
            try:
                await asyncio.wait_for(
                    asyncio.shield(close_task),
                    timeout=_REMOTE_PAIRING_CLOSE_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logger.warning("RemotePairing service close timed out; cancelling")
            except asyncio.CancelledError:
                # The runner itself is being cancelled.  Still cancel and
                # retrieve the owned close task before allowing the runner to
                # finish, otherwise asyncio will report another pending task.
                caller_cancelled = True
        finally:
            if not close_task.done():
                close_task.cancel()
            try:
                await asyncio.wait_for(
                    asyncio.shield(close_task),
                    timeout=_REMOTE_PAIRING_CLOSE_DRAIN_TIMEOUT,
                )
            except (asyncio.TimeoutError, asyncio.CancelledError):
                if not close_task.done():
                    close_task.cancel()
            if close_task.done():
                try:
                    close_task.result()
                except asyncio.CancelledError:
                    pass
                except Exception:
                    logger.debug("RemotePairing service close failed", exc_info=True)

        if caller_cancelled:
            raise asyncio.CancelledError

    async def _finish_task(
        self,
        task: asyncio.Task,
        *,
        cancel: bool = False,
    ) -> BaseException | None:
        """Finish and retrieve one owned task before dropping its reference."""
        if cancel and not task.done():
            task.cancel()
        result = (await asyncio.gather(task, return_exceptions=True))[0]
        if self.task is task:
            self.task = None
        return result if isinstance(result, BaseException) else None

    async def _run_process(self, udid: str, ip: str, port: int) -> None:
        from core.userspace_process import UserspaceTunnelProcess
        from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService

        process = UserspaceTunnelProcess()
        waiters = []
        try:
            info = await process.start(udid, ip, port)
            self.rsd = RemoteServiceDiscoveryService(
                (info['rsd_address'], info['rsd_port']), open_connection=process.dial,
            )
            await self.rsd.connect()
            self.info = dict(info)
            self._ready.set()
            logger.info('Isolated userspace tunnel ready for %s at %s:%d', udid, ip, port)
            waiters = [asyncio.create_task(self._stop.wait()),
                       asyncio.create_task(process.wait_closed())]
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            if not self._stop.is_set():
                logger.warning('Userspace tunnel process ended for %s', udid)
        except BaseException as exc:
            self._error = exc
            self._ready.set()
            raise
        finally:
            # Unpublish readiness before the first teardown await. The task
            # remains alive while draining, but must no longer be adopted.
            rsd_to_close = self.rsd
            self.rsd = None
            self.info = None
            async def cleanup():
                for waiter in waiters:
                    waiter.cancel()
                if waiters:
                    await asyncio.gather(*waiters, return_exceptions=True)
                try:
                    if rsd_to_close is not None:
                        try:
                            await asyncio.wait_for(rsd_to_close.close(), timeout=5)
                        except Exception:
                            logger.warning('Isolated RSD close failed for %s', udid, exc_info=True)
                finally:
                    await process.close()
                    self.rsd = None
                    self.info = None

            closing = asyncio.create_task(cleanup())
            cancelled = False
            while True:
                try:
                    await asyncio.shield(closing)
                    break
                except asyncio.CancelledError:
                    cancelled = True
            if cancelled:
                raise asyncio.CancelledError

    async def _run(self, udid: str, ip: str, port: int) -> None:
        if os.getenv('LOCWARP_TUNNEL_TRANSPORT', '').strip().lower() == 'userspace-process':
            await self._run_process(udid, ip, port)
            return
        from pymobiledevice3.remote import tunnel_service
        from pymobiledevice3.remote.tunnel_service import (
            create_core_device_tunnel_service_using_remotepairing,
        )

        service = None
        install_windows_tunnel_hook(tunnel_service)
        userspace_requested = _userspace_tunnel_requested()
        userspace_claimed = False
        try:
            if userspace_requested:
                global _USERSPACE_TUNNEL_ACTIVE
                # pmd-pytcp is process-global and only supports one active
                # userspace tunnel. The check/set has no await between it, so
                # concurrent candidates cannot interleave here on asyncio's
                # single event loop.
                if _USERSPACE_TUNNEL_ACTIVE:
                    raise RuntimeError(
                        "userspace tunnel already active; one iPhone per backend process"
                    )
                _USERSPACE_TUNNEL_ACTIVE = True
                userspace_claimed = True
                from pymobiledevice3.remote.remote_service_discovery import (
                    RemoteServiceDiscoveryService,
                )
                from pymobiledevice3.remote.userspace_tunnel import UserspaceDialPlane

                # The package-level factory consults this flag when the
                # tunnel context creates its link device. It is reset in the
                # outer finally so later kernel-mode starts are unaffected.
                tunnel_service.USE_USERSPACE_TUNNEL = True
                logger.warning(
                    "Using userspace WiFi tunnel for %s; this backend process supports one "
                    "userspace iPhone at a time and does not create a host network adapter",
                    udid,
                )

            logger.info("Connecting to RemotePairing service at %s:%d", ip, port)
            service = await create_core_device_tunnel_service_using_remotepairing(
                udid, ip, port,
            )
            logger.info("RemotePairing connected (identifier=%s)", service.remote_identifier)

            async with service.start_tcp_tunnel() as tunnel:
                try:
                    if userspace_requested:
                        tun = getattr(getattr(tunnel, "client", None), "tun", None)
                        if tun is None:
                            raise RuntimeError(
                                "userspace tunnel did not expose its userspace link"
                            )
                        tun.set_peer(tunnel.address)
                        self._dial_plane = UserspaceDialPlane(tun, tunnel.address)
                        self.rsd = RemoteServiceDiscoveryService(
                            (tunnel.address, tunnel.port),
                            open_connection=self._dial_plane.dial,
                        )
                        await self.rsd.connect()

                    self.info = {
                        "rsd_address": tunnel.address,
                        "rsd_port": tunnel.port,
                        "interface": tunnel.interface,
                        "protocol": str(tunnel.protocol),
                    }
                    if userspace_requested:
                        self.info["transport"] = "userspace"
                    logger.info(
                        "WiFi tunnel established: %s:%d iface=%s transport=%s",
                        tunnel.address,
                        tunnel.port,
                        tunnel.interface,
                        "userspace" if userspace_requested else "kernel",
                    )
                    self._ready.set()

                    # Wait until either (a) the user requests stop, or (b) the
                    # underlying TCP socket dies. pymobiledevice3's
                    # sock_read_task exits silently on OSError / ConnectionReset
                    # and does NOT propagate out of the start_tcp_tunnel context,
                    # so without this wait_closed() race the runner task hangs
                    # forever after the iPhone goes away.
                    stop_task = asyncio.create_task(self._stop.wait())
                    closed_task = asyncio.create_task(tunnel.client.wait_closed())
                    try:
                        _, pending = await asyncio.wait(
                            [stop_task, closed_task],
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                    finally:
                        for t in (stop_task, closed_task):
                            if not t.done():
                                t.cancel()
                                try:
                                    await t
                                except (asyncio.CancelledError, Exception):
                                    pass

                    if self._stop.is_set():
                        logger.info("Tunnel stop signal received; closing context")
                    else:
                        logger.warning(
                            "Tunnel underlying TCP socket died (sock_read_task exited); "
                            "exiting runner so watchdog can restart"
                        )
                finally:
                    rsd_to_close = self.rsd
                    self.rsd = None
                    self.info = None
                    if rsd_to_close is not None:
                        try:
                            await rsd_to_close.close()
                        except Exception:
                            logger.debug("Userspace RSD close failed", exc_info=True)
                        self.rsd = None
                    if self._dial_plane is not None:
                        try:
                            await self._dial_plane.__aexit__(None, None, None)
                        except Exception:
                            logger.debug("Userspace dial-plane close failed", exc_info=True)
                        self._dial_plane = None
        except BaseException as exc:
            self._error = exc
            self._ready.set()
            raise
        finally:
            # ``start_tcp_tunnel()`` owns only the data tunnel. The
            # RemotePairing control service returned by the factory is a
            # separate socket and must be closed explicitly on every runner
            # exit; otherwise repeated watchdog restarts leave control
            # transports (and their DTX reader tasks) for garbage collection.
            if service is not None:
                try:
                    await self._close_remote_pairing_service(service)
                except BaseException:
                    logger.debug(
                        "RemotePairing service close failed for %s:%d",
                        ip,
                        port,
                        exc_info=True,
                    )
            self.info = None
            self.rsd = None
            self._dial_plane = None
            if userspace_claimed:
                tunnel_service.USE_USERSPACE_TUNNEL = False
                _USERSPACE_TUNNEL_ACTIVE = False

    async def start(self, udid: str, ip: str, port: int, timeout: float = 20.0) -> dict:
        """Start the tunnel and wait until RSD info is ready.

        Raises asyncio.TimeoutError on timeout or the underlying exception
        if the tunnel setup failed before becoming ready.
        """
        async with self.lock:
            if self.is_running():
                raise RuntimeError("TunnelRunner is already running")
            if self.task is not None:
                await self._finish_task(self.task)

            self._stop = asyncio.Event()
            self._ready = asyncio.Event()
            self._error = None
            self.info = None
            self.target_ip = ip
            self.target_port = port
            task = asyncio.create_task(self._run(udid, ip, port))
            self.task = task
            try:
                await asyncio.wait_for(self._ready.wait(), timeout=timeout)
            except BaseException as exc:
                self._stop.set()
                task_error = await self._finish_task(task, cancel=True)
                if isinstance(exc, asyncio.TimeoutError) and isinstance(task_error, HostTunnelError):
                    raise task_error from exc
                raise
            if self._error is not None:
                exc = self._error
                await self._finish_task(task)
                raise exc
            return dict(self.info or {})

    async def stop(self) -> None:
        async with self.lock:
            task = self.task
            if task is None:
                self.info = None
                return

            caller_cancelled: asyncio.CancelledError | None = None
            if not task.done():
                self._stop.set()
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
                except asyncio.TimeoutError:
                    logger.warning("Tunnel task did not exit in 5s; cancelling")
                except asyncio.CancelledError as exc:
                    current = asyncio.current_task()
                    if current is not None and current.cancelling():
                        caller_cancelled = exc
                except Exception:
                    pass

            task_error = await self._finish_task(task, cancel=not task.done())
            self.info = None
            if task_error is not None and not isinstance(
                task_error,
                asyncio.CancelledError,
            ):
                logger.warning(
                    "Tunnel task ended with error during stop: %s: %s",
                    type(task_error).__name__,
                    task_error,
                )
            if caller_cancelled is not None:
                raise caller_cancelled

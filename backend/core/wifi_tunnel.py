"""In-process WiFi tunnel runner.

Backend now runs on Python 3.13 (native TLS-PSK support), so the tunnel
lives inside the backend event loop instead of a separate helper exe.
The tunnel context (`service.start_tcp_tunnel()`) must stay open for the
RSD link to remain usable, so we hold it inside a long-running task and
release it via a stop event.
"""

import asyncio
import logging

logger = logging.getLogger("wifi_tunnel")


class TunnelRunner:
    """Owns the tunnel asyncio task and its RSD info."""

    def __init__(self) -> None:
        self.info: dict | None = None
        self.task: asyncio.Task | None = None
        self.lock = asyncio.Lock()
        self._stop: asyncio.Event = asyncio.Event()
        self._ready: asyncio.Event = asyncio.Event()
        self._error: BaseException | None = None
        # Original (ip, port) the runner was launched against. Useful so
        # callers can tell "is the running tunnel actually for the same
        # iPhone the user is trying to connect to right now?" without
        # having to re-resolve the udid.
        self.target_ip: str | None = None
        self.target_port: int | None = None

    def is_running(self) -> bool:
        return self.task is not None and not self.task.done()

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

    async def _run(self, udid: str, ip: str, port: int) -> None:
        from pymobiledevice3.remote.tunnel_service import (
            create_core_device_tunnel_service_using_remotepairing,
        )
        try:
            logger.info("Connecting to RemotePairing service at %s:%d", ip, port)
            service = await create_core_device_tunnel_service_using_remotepairing(
                udid, ip, port,
            )
            logger.info("RemotePairing connected (identifier=%s)", service.remote_identifier)

            async with service.start_tcp_tunnel() as tunnel:
                self.info = {
                    "rsd_address": tunnel.address,
                    "rsd_port": tunnel.port,
                    "interface": tunnel.interface,
                    "protocol": str(tunnel.protocol),
                }
                logger.info(
                    "WiFi tunnel established: %s:%d iface=%s",
                    tunnel.address, tunnel.port, tunnel.interface,
                )
                self._ready.set()

                # Wait until either (a) the user requests stop, or (b) the
                # underlying TCP socket dies. pymobiledevice3's sock_read_task
                # exits silently on OSError / ConnectionReset and does NOT
                # propagate out of the start_tcp_tunnel context, so without
                # this wait_closed() race the runner task hangs forever even
                # after the iPhone has gone away. _per_tunnel_watchdog only
                # restarts when runner.task ends, so detecting tunnel death
                # here is what wires up the existing auto-restart machinery.
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
        except BaseException as exc:
            self._error = exc
            self._ready.set()
            raise
        finally:
            self.info = None

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
            except BaseException:
                self._stop.set()
                await self._finish_task(task, cancel=True)
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

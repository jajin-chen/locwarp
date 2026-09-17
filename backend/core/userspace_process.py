"""Own one isolated pmd-pytcp process per iPhone."""
import asyncio
import ipaddress
import json
import logging
import secrets
import subprocess
import sys
import threading
from pathlib import Path

logger = logging.getLogger(__name__)
MAX_LINE = 4096


class UserspaceTunnelProcess:
    def __init__(self):
        self.info = None
        self.process = None
        self._token = secrets.token_hex(32)
        self._threads = []
        self._relay_port = None
        self._close_lock = asyncio.Lock()
        self._close_task = None
        self._closing = False
        self._exit_reported = False

    async def start(self, udid, ip, port, timeout=45):
        if getattr(sys, "frozen", False):
            raise RuntimeError("userspace process transport requires source Python; frozen builds are not supported")
        if self.process is not None:
            raise RuntimeError("userspace process already started")
        loop = asyncio.get_running_loop()
        ready = loop.create_future()
        self.process = subprocess.Popen(
            [sys.executable, "-u", "-m", "core.userspace_worker"],
            cwd=str(Path(__file__).resolve().parents[1]),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        process = self.process

        def resolve(line):
            if not ready.done():
                ready.set_result(line)

        def read_ready():
            line = process.stdout.readline(MAX_LINE + 1)
            try:
                loop.call_soon_threadsafe(resolve, line)
            except RuntimeError:
                pass

        def read_logs():
            while line := process.stderr.readline(MAX_LINE):
                logger.warning("userspace child pid=%s: %s", process.pid,
                               line.decode(errors="replace").strip())

        for target in (read_ready, read_logs):
            thread = threading.Thread(target=target, daemon=True)
            self._threads.append(thread)
            thread.start()
        try:
            process.stdin.write((json.dumps({"udid": udid, "ip": ip, "port": port,
                                           "token": self._token}) + "\n").encode())
            process.stdin.flush()
            line = await asyncio.wait_for(ready, timeout)
            if len(line) > MAX_LINE or not line.endswith(b"\n"):
                raise RuntimeError("invalid userspace startup response")
            response = json.loads(line)
            if response.get("status") != "ready":
                raise RuntimeError(response.get("error", "userspace startup failed"))
            ipaddress.ip_address(response["rsd_address"])
            for name in ("rsd_port", "relay_port"):
                if type(response.get(name)) is not int or not 1 <= response[name] <= 65535:
                    raise RuntimeError("invalid userspace startup port")
            self._relay_port = response["relay_port"]
            self.info = {name: response[name] for name in
                         ("rsd_address", "rsd_port", "interface", "protocol")}
            self.info["transport"] = "userspace-process"
            self.info["process_pid"] = process.pid
            return self.info
        except BaseException:
            cleanup = asyncio.create_task(self.close())
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    continue
            cleanup.result()
            raise

    async def dial(self, host, port, **kwargs):
        if not self.info or self.process.poll() is not None:
            raise ConnectionError("userspace process is not running")
        if ipaddress.ip_address(host) != ipaddress.ip_address(self.info["rsd_address"]):
            raise ValueError("userspace relay only accepts its own device address")
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("invalid device port")
        allowed = {"ssl", "server_hostname", "ssl_handshake_timeout", "ssl_shutdown_timeout", "limit"}
        if set(kwargs) - allowed:
            raise TypeError("unsupported userspace dial options")
        ssl = kwargs.pop("ssl", None)
        limit = kwargs.pop("limit", 65536)
        reader, writer = await asyncio.open_connection("127.0.0.1", self._relay_port, limit=limit)
        try:
            writer.write((json.dumps({"token": self._token, "port": port}) + "\n").encode())
            await writer.drain()
            response = await asyncio.wait_for(reader.readuntil(b"\n"), 15)
            if response != b'{"status":"ok"}\n':
                raise ConnectionError("userspace device connection failed")
            if ssl:
                import ssl as ssl_module
                context = ssl_module.create_default_context() if ssl is True else ssl
                kwargs.setdefault("server_hostname", host)
                await writer.start_tls(context, **kwargs)
            elif kwargs:
                raise ValueError("TLS options require ssl")
            return reader, writer
        except BaseException:
            writer.close()
            await writer.wait_closed()
            raise

    async def wait_closed(self):
        while self.process is not None and self.process.poll() is None:
            await asyncio.sleep(0.1)
        if self.process is not None and not self._closing and not self._exit_reported:
            self._exit_reported = True
            logger.warning("Userspace child pid=%s exited unexpectedly with code=%s",
                           self.process.pid, self.process.poll())

    async def close(self):
        self._closing = True
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        cancelled = False
        while not self._close_task.done():
            try:
                await asyncio.shield(self._close_task)
            except asyncio.CancelledError:
                cancelled = True
        self._close_task.result()
        if cancelled:
            raise asyncio.CancelledError

    async def _close(self):
        async with self._close_lock:
            process = self.process
            if process is None:
                return
            if process.stdin and not process.stdin.closed:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            for seconds, action in ((3, None), (2, process.terminate), (2, process.kill)):
                if process.poll() is not None:
                    break
                if action:
                    try:
                        action()
                    except OSError:
                        pass
                deadline = asyncio.get_running_loop().time() + seconds
                while process.poll() is None and asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(0.05)
            if process.poll() is None:
                raise RuntimeError("userspace child could not be stopped")
            for thread in self._threads:
                await asyncio.to_thread(thread.join, 1)
            for pipe in (process.stdout, process.stderr):
                pipe.close()
            self.info = None
            self._relay_port = None

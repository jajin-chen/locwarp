"""Isolated transport ownership and localhost boundary tests."""
import asyncio
import io
import json
import threading

import pytest
from core.userspace_process import UserspaceTunnelProcess
from core.userspace_worker import relay


class FakeProcess:
    pid = 1234
    def __init__(self, output):
        self.stdout = io.BytesIO(output)
        self.stderr = io.BytesIO()
        self.stdin = io.BytesIO()
        self.code = None
    def poll(self):
        if self.stdin.closed:
            self.code = 0
        return self.code
    def terminate(self):
        self.code = -1
    def kill(self):
        self.code = -9


def ready_line(**updates):
    data = dict(status="ready", rsd_address="fd00::1", rsd_port=58783,
                relay_port=12345, interface="userspace", protocol="tcp")
    data.update(updates)
    return (json.dumps(data) + "\n").encode()


async def test_start_uses_private_stdin_and_returns_identity(monkeypatch):
    process = FakeProcess(ready_line())
    calls = []
    def popen(*args, **kwargs):
        calls.append((args, kwargs))
        return process
    monkeypatch.setattr("core.userspace_process.subprocess.Popen", popen)
    owner = UserspaceTunnelProcess()
    info = await owner.start("phone-a", "192.0.2.1", 5000)
    assert json.loads(process.stdin.getvalue())["token"] == owner._token
    assert owner._token not in str(calls)
    assert info["transport"] == "userspace-process"
    assert info["process_pid"] == 1234
    await owner.close()
    assert process.poll() == 0
    assert all(not thread.is_alive() for thread in owner._threads)
    await owner.close()


@pytest.mark.parametrize("output", [b"", b"x" * 4097, ready_line(relay_port=True),
                                    ready_line(rsd_address="untrusted.host"),
                                    b'{"status":"error","error":"PairingError: denied"}\n'])
async def test_bad_startup_cleans_process(monkeypatch, output):
    process = FakeProcess(output)
    monkeypatch.setattr("core.userspace_process.subprocess.Popen", lambda *a, **k: process)
    owner = UserspaceTunnelProcess()
    with pytest.raises((RuntimeError, ValueError)):
        await owner.start("phone", "192.0.2.1", 5000)
    assert process.poll() == 0
    assert all(not thread.is_alive() for thread in owner._threads)


async def test_cancelled_startup_drains_reader(monkeypatch):
    stopped = threading.Event()
    class BlockingOutput(io.BytesIO):
        def readline(self, size):
            stopped.wait(2)
            return b""
    class Input(io.BytesIO):
        def close(self):
            stopped.set()
            super().close()
    process = FakeProcess(b"")
    process.stdout = BlockingOutput()
    process.stdin = Input()
    monkeypatch.setattr("core.userspace_process.subprocess.Popen", lambda *a, **k: process)
    owner = UserspaceTunnelProcess()
    task = asyncio.create_task(owner.start("phone", "192.0.2.1", 5000))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.poll() == 0
    assert all(not thread.is_alive() for thread in owner._threads)


@pytest.mark.parametrize("payload", [[], {"token": "bad", "port": 5000},
                                     {"token": "a" * 64, "port": True},
                                     {"token": "a" * 64, "port": 65536}])
async def test_relay_rejects_bad_auth_and_port(payload):
    calls, tasks = [], []
    async def dial(host, port):
        calls.append((host, port))
        raise AssertionError("should not dial")
    def accepted(reader, writer):
        tasks.append(asyncio.create_task(relay(reader, writer, token="a" * 64,
                                               address="fd00::1", dial=dial)))
    async with await asyncio.start_server(accepted, "127.0.0.1", 0, limit=4096) as server:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1])
        writer.write((json.dumps(payload) + "\n").encode())
        await writer.drain()
        assert await asyncio.wait_for(reader.read(), 1) == b""
        writer.close()
        await writer.wait_closed()
        await asyncio.gather(*tasks)
    assert calls == []


async def test_parent_real_relay_roundtrip_and_address_boundary():
    relay_tasks, echo_tasks, calls = [], [], []
    async def echo(reader, writer):
        try:
            while data := await reader.read(1024):
                writer.write(data)
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
    def accepted_echo(reader, writer):
        echo_tasks.append(asyncio.create_task(echo(reader, writer)))
    async with await asyncio.start_server(accepted_echo, "127.0.0.1", 0) as echo_server:
        async def dial(host, port):
            calls.append((host, port))
            return await asyncio.open_connection("127.0.0.1", echo_server.sockets[0].getsockname()[1])
        owner = UserspaceTunnelProcess()
        owner.process = FakeProcess(b"")
        owner.info = {"rsd_address": "fd00::1"}
        def accepted(reader, writer):
            relay_tasks.append(asyncio.create_task(relay(reader, writer, token=owner._token,
                                                          address="fd00::1", dial=dial)))
        async with await asyncio.start_server(accepted, "127.0.0.1", 0, limit=4096) as server:
            owner._relay_port = server.sockets[0].getsockname()[1]
            with pytest.raises(ValueError, match="own device"):
                await owner.dial("127.0.0.1", 1234)
            with pytest.raises(TypeError, match="unsupported"):
                await owner.dial("fd00::1", 1234, local_addr=("0.0.0.0", 0))
            reader, writer = await owner.dial("fd00::1", 1234)
            writer.write(b"hello device")
            await writer.drain()
            assert await asyncio.wait_for(reader.readexactly(12), 1) == b"hello device"
            assert calls == [("fd00::1", 1234)]
            writer.close()
            await writer.wait_closed()
            await asyncio.wait_for(asyncio.gather(*relay_tasks), 3)
        await asyncio.wait_for(asyncio.gather(*echo_tasks), 3)


async def test_tls_upgrade_happens_after_authenticated_relay(monkeypatch):
    calls = []
    class Writer:
        def write(self, data):
            calls.append(("write", json.loads(data)))
        async def drain(self):
            calls.append(("drain",))
        async def start_tls(self, context, **kwargs):
            calls.append(("tls", context, kwargs))
    reader = asyncio.StreamReader()
    reader.feed_data(b'{"status":"ok"}\n')
    writer = Writer()
    async def connection(host, port, **kwargs):
        assert host == "127.0.0.1"
        assert "ssl" not in kwargs
        return reader, writer
    monkeypatch.setattr(asyncio, "open_connection", connection)
    owner = UserspaceTunnelProcess()
    owner.process = FakeProcess(b"")
    owner.info = {"rsd_address": "fd00::1"}
    owner._relay_port = 12345
    tls_context = object()
    result = await owner.dial("fd00::1", 443, ssl=tls_context, server_hostname="device")
    assert result == (reader, writer)
    assert calls[0][1]["token"] == owner._token
    assert calls[-1] == ("tls", tls_context, {"server_hostname": "device"})


async def test_repeated_close_cancellation_still_reaps_child(monkeypatch):
    process = FakeProcess(ready_line())
    def poll():
        return process.code
    process.poll = poll
    monkeypatch.setattr("core.userspace_process.subprocess.Popen", lambda *a, **k: process)
    owner = UserspaceTunnelProcess()
    await owner.start("phone", "192.0.2.1", 5000)
    task = asyncio.create_task(owner.close())
    await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.sleep(0.01)
    task.cancel()
    process.code = 0
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.stdout.closed
    assert process.stderr.closed
    assert all(not thread.is_alive() for thread in owner._threads)
    await owner.close()

async def test_parent_eof_cancels_pairing_before_ready(monkeypatch):
    from pymobiledevice3.remote import tunnel_service
    from core.userspace_worker import _run_until_parent_closed
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    async def pairing(*args):
        entered.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()
    monkeypatch.setattr(tunnel_service, "create_core_device_tunnel_service_using_remotepairing", pairing)
    monkeypatch.setattr(tunnel_service, "USE_USERSPACE_TUNNEL", False)
    parent_eof = asyncio.Event()
    task = asyncio.create_task(_run_until_parent_closed(
        {"udid": "phone", "ip": "192.0.2.1", "port": 5000}, parent_eof))
    await entered.wait()
    parent_eof.set()
    await asyncio.wait_for(task, 1)
    assert cancelled.is_set()


async def test_parent_eof_closes_service_during_tunnel_startup(monkeypatch):
    from contextlib import asynccontextmanager
    from pymobiledevice3.remote import tunnel_service
    from core.userspace_worker import _run_until_parent_closed
    entered = asyncio.Event()
    closed = asyncio.Event()
    class Service:
        @asynccontextmanager
        async def start_tcp_tunnel(self):
            entered.set()
            await asyncio.Future()
            yield
        async def close(self):
            closed.set()
    async def pairing(*args):
        return Service()
    monkeypatch.setattr(tunnel_service, "create_core_device_tunnel_service_using_remotepairing", pairing)
    monkeypatch.setattr(tunnel_service, "USE_USERSPACE_TUNNEL", False)
    parent_eof = asyncio.Event()
    task = asyncio.create_task(_run_until_parent_closed(
        {"udid": "phone", "ip": "192.0.2.1", "port": 5000}, parent_eof))
    await entered.wait()
    parent_eof.set()
    await asyncio.wait_for(task, 1)
    assert closed.is_set()

async def test_frozen_build_rejected_before_process_launch(monkeypatch):
    import sys
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    def forbidden(*args, **kwargs):
        pytest.fail("frozen app must never relaunch itself with -m")
    monkeypatch.setattr("core.userspace_process.subprocess.Popen", forbidden)
    with pytest.raises(RuntimeError, match="source Python"):
        await UserspaceTunnelProcess().start("phone", "192.0.2.1", 5000)

async def test_unexpected_process_exit_reported_once(caplog):
    import logging
    owner = UserspaceTunnelProcess()
    owner.process = FakeProcess(b"")
    owner.process.code = 1
    with caplog.at_level(logging.WARNING, logger="core.userspace_process"):
        await owner.wait_closed()
        await owner.wait_closed()
    assert caplog.text.count("exited unexpectedly with code=1") == 1
    await owner.close()


async def test_expected_process_close_not_reported_as_failure(caplog):
    import logging
    owner = UserspaceTunnelProcess()
    owner.process = FakeProcess(b"")
    with caplog.at_level(logging.WARNING, logger="core.userspace_process"):
        await owner.close()
        await owner.wait_closed()
    assert "exited unexpectedly" not in caplog.text

async def test_transport_wait_failure_propagates_and_closes_resources(monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from pymobiledevice3.remote import tunnel_service, userspace_tunnel
    from core.userspace_worker import _session
    closed = []
    class Plane:
        def __init__(self, *args):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            closed.append("plane")
    async def transport_closed():
        raise OSError("transport read failed")
    tunnel = SimpleNamespace(address="fd00::1", port=12345, interface="fake", protocol="TCP",
                             client=SimpleNamespace(tun=SimpleNamespace(set_peer=lambda value: None),
                                                    wait_closed=transport_closed))
    class Service:
        @asynccontextmanager
        async def start_tcp_tunnel(self):
            try:
                yield tunnel
            finally:
                closed.append("tunnel")
        async def close(self):
            closed.append("service")
    async def pairing(*args):
        return Service()
    monkeypatch.setattr(tunnel_service, "create_core_device_tunnel_service_using_remotepairing", pairing)
    monkeypatch.setattr(tunnel_service, "USE_USERSPACE_TUNNEL", False)
    monkeypatch.setattr(userspace_tunnel, "UserspaceDialPlane", Plane)
    with pytest.raises(OSError, match="transport read failed"):
        await _session({"udid": "phone", "ip": "192.0.2.1", "port": 5000,
                        "token": "a" * 64}, asyncio.Event())
    assert closed == ["plane", "tunnel", "service"]

def test_child_natural_exit_with_parent_stdin_still_open():
    """Remote transport EOF must not crash finalization on a live stdin watcher."""
    import subprocess
    import sys
    from pathlib import Path
    code = '''
import asyncio
from core import userspace_worker as worker
async def finished_session(config, stop):
    await asyncio.sleep(0.15)
worker._session = finished_session
asyncio.run(worker.run({}))
print("clean-exit", flush=True)
'''
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", code],
        cwd=str(Path(__file__).resolve().parents[1]),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        # Deliberately keep stdin open: this is the real device-EOF path,
        # unlike parent-requested shutdown where stdin already reached EOF.
        code = process.wait(timeout=8)
        stdout = process.stdout.read().decode(errors="replace")
        stderr = process.stderr.read().decode(errors="replace")
        assert code == 0, stderr
        assert "clean-exit" in stdout
        assert "Fatal Python error" not in stderr
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
        for pipe in (process.stdin, process.stdout, process.stderr):
            pipe.close()

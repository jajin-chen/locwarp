"""Process transport must retain per-device RSD and child ownership."""

import asyncio
import sys
from types import SimpleNamespace

import pytest

from core.wifi_tunnel import TunnelRunner


async def test_process_transport_keeps_two_independent_devices(monkeypatch):
    processes = []
    rsds = []

    class Process:
        def __init__(self):
            self.closed = False
            self.done = asyncio.Event()
            processes.append(self)

        async def start(self, udid, ip, port):
            self.info = dict(rsd_address=udid, rsd_port=1234,
                             interface='userspace', transport='userspace-process')
            return self.info

        async def dial(self, *args, **kwargs):
            pass

        async def wait_closed(self):
            await self.done.wait()

        async def close(self):
            self.closed = True
            self.done.set()

    class RSD:
        def __init__(self, address, open_connection):
            self.address = address
            self.dial = open_connection
            self.closed = False
            rsds.append(self)

        async def connect(self):
            pass

        async def close(self):
            self.closed = True

    monkeypatch.setenv('LOCWARP_TUNNEL_TRANSPORT', 'userspace-process')
    monkeypatch.setitem(sys.modules, 'core.userspace_process',
                        SimpleNamespace(UserspaceTunnelProcess=Process))
    from pymobiledevice3.remote import remote_service_discovery
    monkeypatch.setattr(remote_service_discovery, 'RemoteServiceDiscoveryService', RSD)
    first, second = TunnelRunner(), TunnelRunner()
    try:
        await asyncio.gather(first.start('one', '192.0.2.1', 1),
                             second.start('two', '192.0.2.2', 2))
        assert first.rsd is not second.rsd
        assert first.is_running() and second.is_running()
        await first.stop()
        assert processes[0].closed and rsds[0].closed
        assert second.is_running() and not processes[1].closed
    finally:
        await first.stop()
        await second.stop()
    assert all(p.closed for p in processes)
    assert all(r.closed for r in rsds)


async def test_process_cleanup_survives_repeated_cancellation(monkeypatch):
    closing = asyncio.Event()
    release = asyncio.Event()
    closed = asyncio.Event()

    class Process:
        async def start(self, *_args):
            await asyncio.Event().wait()

        async def close(self):
            closing.set()
            await release.wait()
            closed.set()

    monkeypatch.setenv('LOCWARP_TUNNEL_TRANSPORT', 'userspace-process')
    monkeypatch.setitem(sys.modules, 'core.userspace_process',
                        SimpleNamespace(UserspaceTunnelProcess=Process))
    runner = TunnelRunner()
    task = asyncio.create_task(runner._run('one', '192.0.2.1', 1))
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.wait_for(closing.wait(), 1)
    task.cancel()
    await asyncio.sleep(0)
    assert not closed.is_set() and not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()


async def test_process_unpublishes_rsd_before_slow_close(monkeypatch):
    closed = asyncio.Event()
    closing = asyncio.Event()
    release = asyncio.Event()

    class Process:
        async def start(self, *_args):
            return dict(rsd_address='::1', rsd_port=1234)

        async def dial(self, *_args, **_kwargs):
            pass

        async def wait_closed(self):
            await closed.wait()

        async def close(self):
            pass

    class RSD:
        def __init__(self, *_args, **_kwargs):
            pass

        async def connect(self):
            pass

        async def close(self):
            closing.set()
            await release.wait()

    monkeypatch.setenv('LOCWARP_TUNNEL_TRANSPORT', 'userspace-process')
    monkeypatch.setitem(sys.modules, 'core.userspace_process',
                        SimpleNamespace(UserspaceTunnelProcess=Process))
    from pymobiledevice3.remote import remote_service_discovery
    monkeypatch.setattr(remote_service_discovery, 'RemoteServiceDiscoveryService', RSD)
    runner = TunnelRunner()
    try:
        await runner.start('one', '192.0.2.1', 1)
        assert runner.rsd is not None
        closed.set()
        await asyncio.wait_for(closing.wait(), 1)
        assert runner.is_running()  # Still draining, but no adoptable RSD.
        assert runner.rsd is None
        assert runner.info is None
    finally:
        release.set()
        await runner.stop()

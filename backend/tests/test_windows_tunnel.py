import asyncio
import threading
from types import SimpleNamespace

import pytest

from core.windows_tunnel import HostTunnelError, _start_windows_tunnel, install_windows_tunnel_hook
from core.wifi_tunnel import TunnelRunner


@pytest.mark.parametrize("fail", [False, True])
async def test_native_create_does_not_block_loop_and_cancellation_drains(fail):
    entered = threading.Event()
    release = threading.Event()
    closed = threading.Event()
    adapter = SimpleNamespace(up=lambda: None, close=closed.set)

    def create(_name):
        entered.set()
        release.wait(2)
        if fail:
            raise OSError(4319, "WintunCreateAdapter failed")
        return adapter

    socket_closed = False

    async def stop():
        nonlocal socket_closed
        socket_closed = True

    client = SimpleNamespace(tun=None, stop_tunnel=stop)
    task = asyncio.create_task(_start_windows_tunnel(
        client, SimpleNamespace(TunTapDevice=create), "fd00::1", 1500, "test",
    ))
    try:
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(0.001)
        assert entered.is_set()
        assert not task.done()
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(HostTunnelError if fail else asyncio.CancelledError) as caught:
        await task
    assert client.tun is None
    if fail:
        assert caught.value.winerror == 4319
    else:
        assert closed.is_set()
        assert socket_closed


async def test_runner_timeout_preserves_host_failure_during_native_drain(monkeypatch):
    error = HostTunnelError(OSError(4319, "adapter failed"))

    async def run(self, *_args):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise error

    monkeypatch.setattr(TunnelRunner, "_run", run)
    runner = TunnelRunner()
    with pytest.raises(HostTunnelError) as caught:
        await runner.start("phone", "192.0.2.1", 50000, timeout=0.01)
    assert caught.value is error
    assert runner.task is None


async def test_configuration_failure_closes_partial_adapter():
    closed = []

    def fail_up():
        raise OSError(5, "access denied")

    adapter = SimpleNamespace(up=fail_up, close=lambda: closed.append(True))
    with pytest.raises(HostTunnelError) as caught:
        await _start_windows_tunnel(SimpleNamespace(tun=None), SimpleNamespace(
            TunTapDevice=lambda _name: adapter,
        ), "fd00::1", 1500, "test")
    assert caught.value.winerror == 5
    assert closed == [True]


async def test_hook_is_idempotent_and_preserves_userspace(monkeypatch):
    import core.windows_tunnel as windows_tunnel

    calls = []

    class Client:
        async def start_tunnel(self, *args):
            calls.append(args)

    module = SimpleNamespace(
        RemotePairingTunnel=Client, DEFAULT_INTERFACE_NAME="test", USE_USERSPACE_TUNNEL=True,
    )
    monkeypatch.setattr(windows_tunnel.sys, "platform", "win32")
    install_windows_tunnel_hook(module)
    hook = Client.start_tunnel
    install_windows_tunnel_hook(module)
    assert Client.start_tunnel is hook
    await Client().start_tunnel("fd00::1", 1500)
    assert calls == [("fd00::1", 1500, "test")]

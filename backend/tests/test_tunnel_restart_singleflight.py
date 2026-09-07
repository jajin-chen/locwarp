"""Concurrent recovery requests share one runner start per generation."""

import asyncio

import pytest

import services.tunnel_manager as tm

pytestmark = pytest.mark.asyncio


@pytest.fixture
def registry(monkeypatch):
    monkeypatch.setattr(tm, "_tunnels", {})
    monkeypatch.setattr(tm, "_tunnel_generations", {})
    monkeypatch.setattr(tm, "_tunnels_lock", asyncio.Lock())


def seed(udid):
    runner = object()
    tm._tunnels[udid] = runner
    tm._tunnel_generations[udid] = 1
    return runner


async def test_same_generation_shares_restart_result(monkeypatch, registry):
    runner = seed("phone")
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def restart(*args, **kwargs):
        calls.append(args)
        entered.set()
        await release.wait()
        return True

    monkeypatch.setattr(tm, "_attempt_tunnel_restart_impl", restart)
    first = asyncio.create_task(tm._attempt_tunnel_restart("phone", "ip", 1, None, runner))
    await entered.wait()
    second = asyncio.create_task(tm._attempt_tunnel_restart("phone", "ip", 1, None, runner))
    await asyncio.sleep(0)
    release.set()
    assert await asyncio.gather(first, second) == [True, True]
    assert len(calls) == 1


async def test_cancelled_waiter_does_not_cancel_owner(monkeypatch, registry):
    runner = seed("phone")
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def restart(*args, **kwargs):
        calls.append(args)
        entered.set()
        await release.wait()
        return True

    monkeypatch.setattr(tm, "_attempt_tunnel_restart_impl", restart)
    owner = asyncio.create_task(tm._attempt_tunnel_restart("phone", "ip", 1, None, runner))
    await entered.wait()
    waiter = asyncio.create_task(tm._attempt_tunnel_restart("phone", "ip", 1, None, runner))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    release.set()
    assert await owner is True
    assert len(calls) == 1


async def test_owner_cancellation_reaches_cleanup_and_waiter(monkeypatch, registry):
    runner = seed("phone")
    entered, cleaned = asyncio.Event(), asyncio.Event()

    async def restart(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    monkeypatch.setattr(tm, "_attempt_tunnel_restart_impl", restart)
    owner = asyncio.create_task(tm._attempt_tunnel_restart("phone", "ip", 1, None, runner))
    await entered.wait()
    waiter = asyncio.create_task(tm._attempt_tunnel_restart("phone", "ip", 1, None, runner))
    await asyncio.sleep(0)
    owner.cancel()
    results = await asyncio.wait_for(asyncio.gather(owner, waiter, return_exceptions=True), 1)
    assert all(isinstance(result, asyncio.CancelledError) for result in results)
    assert cleaned.is_set()


async def test_other_devices_and_new_generations_are_independent(monkeypatch, registry):
    runner = seed("phone")
    other = seed("other")
    entered = asyncio.Queue()
    release = asyncio.Event()

    async def restart(udid, *args, **kwargs):
        entered.put_nowait(udid)
        await release.wait()
        return True

    monkeypatch.setattr(tm, "_attempt_tunnel_restart_impl", restart)
    first = asyncio.create_task(tm._attempt_tunnel_restart("phone", "ip", 1, None, runner))
    assert await entered.get() == "phone"
    second = asyncio.create_task(tm._attempt_tunnel_restart("other", "ip2", 2, None, other))
    assert await asyncio.wait_for(entered.get(), 1) == "other"
    tm._tunnel_generations["phone"] += 1
    third = asyncio.create_task(tm._attempt_tunnel_restart("phone", "ip", 1, None, runner))
    assert await asyncio.wait_for(entered.get(), 1) == "phone"
    release.set()
    assert await asyncio.gather(first, second, third) == [True, True, True]


async def test_stopped_original_does_not_start_again(monkeypatch, registry):
    runner = seed("phone")
    del tm._tunnels["phone"]

    async def restart(*args, **kwargs):
        pytest.fail("A detached original must not start another tunnel")

    monkeypatch.setattr(tm, "_attempt_tunnel_restart_impl", restart)
    assert await tm._attempt_tunnel_restart("phone", "ip", 1, None, runner) is False

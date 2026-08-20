"""Regression tests for TunnelRunner task ownership."""

from __future__ import annotations

import asyncio
import gc
import logging

import pytest

from core.wifi_tunnel import TunnelRunner


async def test_setup_failure_is_retrieved_before_task_reference_is_cleared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pymobiledevice3.remote import tunnel_service

    setup_error = RuntimeError("remote pairing failed")

    async def fail_setup(*_args, **_kwargs):
        raise setup_error

    monkeypatch.setattr(
        tunnel_service,
        "create_core_device_tunnel_service_using_remotepairing",
        fail_setup,
    )

    loop = asyncio.get_running_loop()
    old_handler = loop.get_exception_handler()
    unhandled: list[dict] = []
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
    try:
        runner = TunnelRunner()
        with pytest.raises(RuntimeError) as caught:
            await runner.start("udid", "192.0.2.10", 49152, timeout=0.1)

        assert caught.value is setup_error
        assert runner.task is None

        del runner
        gc.collect()
        await asyncio.sleep(0)

        assert not [
            context
            for context in unhandled
            if context.get("message") == "Task exception was never retrieved"
        ]
    finally:
        loop.set_exception_handler(old_handler)


async def test_start_timeout_cancels_and_finishes_child_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pymobiledevice3.remote import tunnel_service

    setup_started = asyncio.Event()
    setup_cancelled = asyncio.Event()

    async def hang_setup(*_args, **_kwargs):
        setup_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            setup_cancelled.set()
            raise

    monkeypatch.setattr(
        tunnel_service,
        "create_core_device_tunnel_service_using_remotepairing",
        hang_setup,
    )

    runner = TunnelRunner()
    with pytest.raises(asyncio.TimeoutError):
        await runner.start("udid", "192.0.2.10", 49152, timeout=0.01)

    assert setup_started.is_set()
    assert setup_cancelled.is_set()
    assert runner.task is None


async def test_cancelling_start_cancels_and_finishes_child_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pymobiledevice3.remote import tunnel_service

    setup_started = asyncio.Event()
    setup_cancelled = asyncio.Event()

    async def hang_setup(*_args, **_kwargs):
        setup_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            setup_cancelled.set()
            raise

    monkeypatch.setattr(
        tunnel_service,
        "create_core_device_tunnel_service_using_remotepairing",
        hang_setup,
    )

    runner = TunnelRunner()
    start_task = asyncio.create_task(
        runner.start("udid", "192.0.2.10", 49152, timeout=60),
    )
    try:
        await setup_started.wait()
        start_task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await start_task

        assert setup_cancelled.is_set()
        assert runner.task is None
    finally:
        if runner.task is not None:
            runner.task.cancel()
            await asyncio.gather(runner.task, return_exceptions=True)


async def test_second_start_is_rejected_while_runner_is_active(monkeypatch) -> None:
    runner = TunnelRunner()

    async def fake_run(*_args) -> None:
        runner.info = {"rsd_address": "fd00::1", "rsd_port": 1234}
        runner._ready.set()
        await runner._stop.wait()

    monkeypatch.setattr(runner, "_run", fake_run)

    await runner.start("udid", "192.0.2.10", 49152)
    with pytest.raises(RuntimeError, match="already running"):
        await runner.start("udid", "192.0.2.10", 49152)
    await runner.stop()


async def test_stop_retrieves_and_logs_completed_task_error(caplog) -> None:
    runner = TunnelRunner()

    async def fail() -> None:
        raise RuntimeError("tunnel failed before stop")

    task = asyncio.create_task(fail())
    runner.task = task
    await asyncio.sleep(0)

    with caplog.at_level(logging.WARNING, logger="wifi_tunnel"):
        await runner.stop()

    assert runner.task is None
    assert "tunnel failed before stop" in caplog.text


async def test_cancelling_stop_cleans_child_and_propagates() -> None:
    runner = TunnelRunner()
    child_started = asyncio.Event()
    child_cancelled = asyncio.Event()

    async def hang() -> None:
        child_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            child_cancelled.set()
            raise

    runner.task = asyncio.create_task(hang())
    await child_started.wait()
    stop_task = asyncio.create_task(runner.stop())
    await asyncio.sleep(0)
    stop_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await stop_task

    assert child_cancelled.is_set()
    assert runner.task is None

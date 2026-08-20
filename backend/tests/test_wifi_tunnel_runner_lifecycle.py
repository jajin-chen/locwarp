"""P1 regressions for runner-task and watchdog cancellation ownership."""

from __future__ import annotations

import asyncio
import gc
import logging

import pytest

import services.tunnel_manager as tunnel_manager


class _ObservedRunner:
    """A runner whose long-lived task reports every watchdog observer."""

    def __init__(self) -> None:
        self._hold = asyncio.Event()
        self._observed = [asyncio.Event(), asyncio.Event()]
        self._await_count = 0
        self.child_task = asyncio.create_task(self._run())
        self.stop_calls = 0
        self.retrieved = False
        self.target_ip = "192.0.2.10"
        self.target_port = 49152

    async def _run(self) -> None:
        await self._hold.wait()

    @property
    def task(self):
        index = min(self._await_count, len(self._observed) - 1)
        self._await_count += 1
        return _ObservedAwaitable(self.child_task, self._observed[index])

    async def wait_until_observed(self, index: int) -> None:
        await asyncio.wait_for(self._observed[index].wait(), timeout=0.5)

    async def stop(self) -> None:
        self.stop_calls += 1
        self._hold.set()
        if not self.child_task.done():
            self.child_task.cancel()
        await asyncio.gather(self.child_task, return_exceptions=True)
        self.retrieved = True


class _ObservedAwaitable:
    def __init__(self, task: asyncio.Task, observed: asyncio.Event) -> None:
        self._task = task
        self._observed = observed

    def __await__(self):
        self._observed.set()
        return self._task.__await__()


class _OwnedRunner:
    def __init__(self) -> None:
        self.child_started = asyncio.Event()
        self.child_task = asyncio.create_task(self._run())
        self.stop_started = asyncio.Event()
        self.stop_calls = 0
        self.retrieved = False

    async def _run(self) -> None:
        self.child_started.set()
        await asyncio.Event().wait()

    async def stop(self) -> None:
        self.stop_calls += 1
        self.stop_started.set()
        if not self.child_task.done():
            self.child_task.cancel()
        await asyncio.gather(self.child_task, return_exceptions=True)
        self.retrieved = True


class _OwnedSideEffect:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.release = asyncio.Event()
        self.task: asyncio.Task | None = None

    async def run(self) -> None:
        self.task = asyncio.current_task()
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


class _BlockingCleanupSideEffect:
    """Side effect that stays in its cancellation cleanup until released."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancel_started = asyncio.Event()
        self.release = asyncio.Event()
        self.task: asyncio.Task | None = None

    async def run(self) -> None:
        self.task = asyncio.current_task()
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancel_started.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                # A caller cancellation must not make this owned child
                # disappear without being retrieved.
                pass
            raise


@pytest.fixture(autouse=True)
async def clean_tunnel_manager_state() -> None:
    async def drain() -> None:
        watchdogs = list(tunnel_manager._tunnel_watchdogs.values())
        for task in watchdogs:
            if not task.done():
                task.cancel()
        if watchdogs:
            await asyncio.gather(*watchdogs, return_exceptions=True)

        side_effects = [
            task
            for tasks in tunnel_manager._tunnel_side_effects.values()
            for task in tasks
        ]
        for task in side_effects:
            if not task.done():
                task.cancel()
        if side_effects:
            await asyncio.gather(*side_effects, return_exceptions=True)

        runners = list(tunnel_manager._tunnels.values())
        for runner in runners:
            stop = getattr(runner, "stop", None)
            if stop is not None:
                await stop()
        tunnel_manager._tunnel_watchdogs.clear()
        tunnel_manager._tunnels.clear()
        tunnel_manager._tunnel_side_effects.clear()
        tunnel_manager._tunnel_generations.clear()

    await drain()
    yield
    await drain()


async def test_cancelled_old_watchdog_does_not_cancel_runner_before_new_watchdog_observes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rekeying watchdog ownership must leave the shared runner task alive."""

    udid = "udid-1"
    runner = _ObservedRunner()
    tunnel_manager._tunnels[udid] = runner  # type: ignore[assignment]
    tunnel_manager._tunnel_generations[udid] = 1
    old_watchdog = asyncio.create_task(
        tunnel_manager._per_tunnel_watchdog(udid, runner, 1),
    )
    tunnel_manager._tunnel_watchdogs[udid] = old_watchdog

    new_watchdog: asyncio.Task | None = None
    try:
        await runner.wait_until_observed(0)
        old_watchdog.cancel()
        await asyncio.gather(old_watchdog, return_exceptions=True)

        assert not runner.child_task.done()

        async with tunnel_manager._tunnels_lock:
            tunnel_manager._tunnel_generations[udid] = 2
            new_watchdog = asyncio.create_task(
                tunnel_manager._per_tunnel_watchdog(udid, runner, 2),
            )
            tunnel_manager._tunnel_watchdogs[udid] = new_watchdog
        await runner.wait_until_observed(1)
        assert not runner.child_task.done()

        detached, watchdog, side_effects = await tunnel_manager._detach_tunnel(
            udid,
            expected=runner,
        )
        assert detached is runner
        assert watchdog is new_watchdog
        await tunnel_manager._stop_tunnel_parts(
            detached,
            watchdog,
            caller="test_runner_rekey_teardown",
            udid=udid,
            side_effects=side_effects,
        )
        assert watchdog.done()
        assert runner.child_task.done()
        assert runner.retrieved
    finally:
        if new_watchdog is not None and not new_watchdog.done():
            new_watchdog.cancel()
        if new_watchdog is not None:
            await asyncio.gather(new_watchdog, return_exceptions=True)
        if not old_watchdog.done():
            old_watchdog.cancel()
        await asyncio.gather(old_watchdog, return_exceptions=True)
        if not runner.child_task.done():
            await runner.stop()


async def test_stop_tunnel_parts_propagates_caller_cancel_after_owned_cleanup() -> None:
    """Caller cancellation must survive watchdog cancellation and cleanup."""

    runner = _OwnedRunner()
    await asyncio.wait_for(runner.child_started.wait(), timeout=0.5)

    watchdog_cleanup_started = asyncio.Event()
    release_watchdog_cleanup = asyncio.Event()

    async def watchdog_body() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            watchdog_cleanup_started.set()
            await release_watchdog_cleanup.wait()
            raise

    watchdog = asyncio.create_task(watchdog_body())
    stop_task = asyncio.create_task(
        tunnel_manager._stop_tunnel_parts(
            runner,
            watchdog,
            caller="test_caller_cancel",
            udid="udid-1",
        ),
    )
    try:
        await asyncio.wait_for(watchdog_cleanup_started.wait(), timeout=0.5)
        stop_task.cancel()
        # A second same-tick cancellation must not bypass the shielded
        # cleanup or turn the caller's cancellation into a swallowed return.
        stop_task.cancel()
        release_watchdog_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(stop_task, timeout=0.5)
        assert watchdog.done()
        assert runner.stop_calls == 1
        assert runner.child_task.done()
        assert runner.retrieved
    finally:
        release_watchdog_cleanup.set()
        if not stop_task.done():
            stop_task.cancel()
        await asyncio.gather(stop_task, return_exceptions=True)
        if not watchdog.done():
            watchdog.cancel()
        await asyncio.gather(watchdog, return_exceptions=True)
        if not runner.child_task.done():
            await runner.stop()


async def test_prior_generation_side_effect_cleanup_does_not_cancel_rekeyed_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A G1 cleanup must leave the rekeyed G2 side effect running."""

    udid = "udid-1"
    old_runner = _OwnedRunner()
    new_runner = _OwnedRunner()
    old_effect = _OwnedSideEffect()
    new_effect = _OwnedSideEffect()
    tunnel_manager._tunnels[udid] = old_runner  # type: ignore[assignment]
    tunnel_manager._tunnel_generations[udid] = 1

    try:
        await tunnel_manager._spawn_owned_tunnel_side_effect(
            udid,
            old_runner,
            1,
            old_effect.run,
        )
        await asyncio.wait_for(old_effect.started.wait(), timeout=0.5)

        async with tunnel_manager._tunnels_lock:
            tunnel_manager._tunnels[udid] = new_runner  # type: ignore[assignment]
            tunnel_manager._tunnel_generations[udid] = 2

        await tunnel_manager._spawn_owned_tunnel_side_effect(
            udid,
            new_runner,
            2,
            new_effect.run,
        )
        await asyncio.wait_for(new_effect.started.wait(), timeout=0.5)

        await tunnel_manager._cancel_tunnel_side_effects(udid, generation=1)
        await asyncio.wait_for(old_effect.cancelled.wait(), timeout=0.5)
        assert old_effect.task is not None
        assert old_effect.task.done()
        assert not new_effect.cancelled.is_set()
        assert new_effect.task is not None
        assert not new_effect.task.done()

        await tunnel_manager._cancel_tunnel_side_effects(udid, generation=2)
        await asyncio.wait_for(new_effect.cancelled.wait(), timeout=0.5)
        assert new_effect.task is not None
        assert new_effect.task.done()
    finally:
        old_effect.release.set()
        new_effect.release.set()
        await old_runner.stop()
        await new_runner.stop()


async def test_cancelled_teardown_drains_owned_side_effect_watchdog_and_runner() -> None:
    """Teardown caller cancellation cannot strand owned lifecycle tasks."""

    udid = "udid-1"
    runner = _OwnedRunner()
    side_effect = _BlockingCleanupSideEffect()
    watchdog_cleanup_started = asyncio.Event()

    async def watchdog_body() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            watchdog_cleanup_started.set()
            raise

    watchdog = asyncio.create_task(watchdog_body())
    tunnel_manager._tunnels[udid] = runner  # type: ignore[assignment]
    tunnel_manager._tunnel_generations[udid] = 1
    tunnel_manager._tunnel_watchdogs[udid] = watchdog
    await tunnel_manager._spawn_owned_tunnel_side_effect(
        udid,
        runner,
        1,
        side_effect.run,
    )
    await asyncio.wait_for(side_effect.started.wait(), timeout=0.5)

    teardown = asyncio.create_task(
        tunnel_manager._tear_down_tunnel(
            udid,
            caller="test_cancelled_teardown",
        ),
    )
    try:
        await asyncio.wait_for(side_effect.cancel_started.wait(), timeout=0.5)
        # The side effect is still in owned cleanup when its caller is
        # cancelled. Release it only after cancellation is observed so this
        # covers the same-tick cleanup boundary.
        teardown.cancel()
        side_effect.release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(teardown, timeout=0.5)

        assert side_effect.task is not None
        assert side_effect.task.done()
        assert watchdog_cleanup_started.is_set()
        assert watchdog.done()
        assert runner.stop_calls == 1
        assert runner.child_task.done()
        assert runner.retrieved
        assert udid not in tunnel_manager._tunnels
        assert udid not in tunnel_manager._tunnel_watchdogs
        assert tunnel_manager._tunnel_side_effects == {}
    finally:
        side_effect.release.set()
        if not teardown.done():
            teardown.cancel()
        await asyncio.gather(teardown, return_exceptions=True)
        if not watchdog.done():
            watchdog.cancel()
        await asyncio.gather(watchdog, return_exceptions=True)
        if not runner.child_task.done():
            await runner.stop()


async def test_owned_side_effect_failure_is_retrieved_with_owner_identity(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failed owned child must be consumed and logged with its lease key.

    The done callback is the only owner left once the side effect exits.  It
    therefore has to call ``task.result()``/``task.exception()`` (rather than
    merely dropping the task from the registry), otherwise asyncio reports a
    late ``Task exception was never retrieved`` warning when the task is
    collected.  Cancellation is intentionally covered by the companion test
    below and must remain quiet.
    """

    udid = "udid-side-effect-error"
    generation = 17
    runner = _OwnedRunner()
    tunnel_manager._tunnels[udid] = runner  # type: ignore[assignment]
    tunnel_manager._tunnel_generations[udid] = generation

    loop = asyncio.get_running_loop()
    unhandled: list[dict] = []
    previous_handler = loop.get_exception_handler()

    def capture_unhandled(_loop: asyncio.AbstractEventLoop, context: dict) -> None:
        unhandled.append(context)

    loop.set_exception_handler(capture_unhandled)
    caplog.set_level(logging.ERROR, logger="wifi_tunnel")

    async def fail() -> None:
        raise RuntimeError("post-setup side effect failed")

    try:
        child = await tunnel_manager._spawn_owned_tunnel_side_effect(
            udid,
            runner,
            generation,
            fail,
        )
        assert child is not None
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert child.done()

        # Do not call child.exception() here: retrieval belongs to the
        # lifecycle callback under test.  Drop our last reference and force
        # finalization so a missing retrieval is deterministic in this test.
        del child
        gc.collect()
        await asyncio.sleep(0)

        assert tunnel_manager._tunnel_side_effects == {}
        assert not any(
            "Task exception was never retrieved" in context.get("message", "")
            for context in unhandled
        )
        owner_logs = [
            record
            for record in caplog.records
            if record.levelno >= logging.ERROR
            and udid in record.getMessage()
            and str(generation) in record.getMessage()
        ]
        assert owner_logs, "side-effect failure log must identify udid and generation"
        assert any(
            record.exc_info is not None
            and isinstance(record.exc_info[1], RuntimeError)
            for record in owner_logs
        )
    finally:
        loop.set_exception_handler(previous_handler)
        if not runner.child_task.done():
            await runner.stop()


async def test_owned_side_effect_cancellation_is_retrieved_without_error_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A normally-cancelled owned child is retrieved without noisy logging."""

    udid = "udid-side-effect-cancel"
    generation = 18
    runner = _OwnedRunner()
    tunnel_manager._tunnels[udid] = runner  # type: ignore[assignment]
    tunnel_manager._tunnel_generations[udid] = generation

    loop = asyncio.get_running_loop()
    unhandled: list[dict] = []
    previous_handler = loop.get_exception_handler()

    def capture_unhandled(_loop: asyncio.AbstractEventLoop, context: dict) -> None:
        unhandled.append(context)

    loop.set_exception_handler(capture_unhandled)
    caplog.set_level(logging.ERROR, logger="wifi_tunnel")

    async def cancel() -> None:
        raise asyncio.CancelledError

    try:
        child = await tunnel_manager._spawn_owned_tunnel_side_effect(
            udid,
            runner,
            generation,
            cancel,
        )
        assert child is not None
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert child.cancelled()
        del child
        gc.collect()
        await asyncio.sleep(0)

        assert tunnel_manager._tunnel_side_effects == {}
        assert not unhandled
        assert not any(
            record.levelno >= logging.ERROR
            and udid in record.getMessage()
            for record in caplog.records
        )
    finally:
        loop.set_exception_handler(previous_handler)
        if not runner.child_task.done():
            await runner.stop()

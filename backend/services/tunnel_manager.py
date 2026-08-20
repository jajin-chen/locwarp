"""Per-device WiFi tunnel lifecycle: registry, watchdog, restart/backoff.

Extracted from api/device.py (v0.2.9x) so that core/device_manager.py can
drive the same tunnel-restart path without importing the FastAPI router
(the old core→api import was a layering inversion papered over with
try/except ImportError). api/device.py keeps the routes and imports the
registry + helpers from here.
"""

import asyncio
from contextlib import asynccontextmanager
import inspect
import ipaddress
import logging
from dataclasses import dataclass, field

from core.wifi_tunnel import TunnelRunner
from services.tunnel_discovery import find_fallback_endpoints

_tunnel_logger = logging.getLogger("wifi_tunnel")


def _dm():
    from main import app_state
    return app_state.device_manager


# Per-device tunnel registry. Each connected iOS 17+ device that uses
# WiFi (instead of USB) gets its own TunnelRunner. v0.2.83 lifted the
# previous singleton design so multiple iPhones can run on WiFi at once.
# Registry mutations go through _tunnels_lock; individual TunnelRunners
# also keep their own .lock for their own lifecycle (start/stop/wait).
_tunnels: dict[str, TunnelRunner] = {}
_tunnel_watchdogs: dict[str, asyncio.Task] = {}
_tunnels_lock = asyncio.Lock()
# API start requests serialize candidate resolution per normalized target IP.
# The state is loop-local because asyncio locks cannot be shared by event
# loops.  Lanes are reference-counted so idle IP keys do not accumulate.
_tunnel_start_lanes_loop = None
_tunnel_start_lanes: dict[str, "_TunnelStartLane"] = {}
# The composite lock is intentionally only an adoption commit lock.  Runner
# handshakes/candidate walks happen outside it; DM lease, engine, registry and
# watchdog ownership changes are committed under it.
#
# Stop routes intentionally do not acquire it so their fence/detach remains
# prompt during a network handshake.
_tunnel_lifecycle_lock: asyncio.Lock | None = None
_tunnel_lifecycle_lock_loop = None


@dataclass(slots=True)
class _TunnelStartLane:
    lock: asyncio.Lock
    refs: int = 0


def _get_tunnel_lifecycle_lock() -> asyncio.Lock:
    """Return a lifecycle lock bound to the currently running event loop."""
    global _tunnel_lifecycle_lock, _tunnel_lifecycle_lock_loop
    loop = asyncio.get_running_loop()
    if _tunnel_lifecycle_lock_loop is not loop:
        _tunnel_lifecycle_lock = asyncio.Lock()
        _tunnel_lifecycle_lock_loop = loop
    return _tunnel_lifecycle_lock


def _normalize_tunnel_ip(value: str | None) -> str:
    """Normalize an IP for start-lane sharing without rejecting hostnames."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        return str(ipaddress.ip_address(raw))
    except ValueError:
        return raw.casefold()


class TunnelStartCancelled(Exception):
    """A start attempt was cancelled by an explicit tunnel stop request."""


class TunnelStartTimedOut(Exception):
    """A start request exhausted its end-to-end deadline before lock entry."""


@dataclass(slots=True)
class TunnelStartAttempt:
    """Lifecycle ownership for one API start, including its unregistered phase."""

    sequence: int
    requested_udid: str | None
    task: asyncio.Task | None
    deadline: float | None = None
    current_udid: str | None = None
    resolved_udid: str | None = None
    registry_key: str | None = None
    runner: TunnelRunner | None = None
    # A same-endpoint idempotent start borrows the currently registered
    # runner.  The follow-up start-and-connect route may re-arm its watchdog,
    # but must never stop the borrowed runner on a later failure.
    runner_owned: bool = True
    # The runner handshake is awaited through a child task so a per-UDID stop
    # can cancel only the active candidate probe and let this route continue
    # with its other pair records.
    probe_task: asyncio.Task | None = None
    probe_udid: str | None = None
    committed: bool = False
    cancel_requested: bool = False
    candidate_exclusions: set[str] = field(default_factory=set)


@dataclass(slots=True)
class TunnelStopPart:
    """Detached lifecycle objects and exact DM lease for one stop target."""

    udid: str
    runner: TunnelRunner | None = None
    watchdog: asyncio.Task | None = None
    side_effects: tuple[asyncio.Task, ...] = ()
    expected_connection: object | None = None
    expected_engine: object | None = None
    generation: int | None = None


@dataclass(slots=True)
class TunnelStopPlan:
    """Linearized stop intent and all objects detached by that intent."""

    target_udid: str | None
    udids: list[str]
    parts: list[TunnelStopPart]
    pending_tasks: tuple[asyncio.Task, ...] = ()
    matched_pending: bool = False


# A start is registered before it waits for its per-IP start lane.  Stop routes
# use the sequence watermarks to fence every older attempt, including a
# handshake which has not entered _tunnels yet.
_tunnel_start_sequence = 0
_tunnel_stop_all_watermark = 0
_tunnel_stop_watermarks: dict[str, int] = {}
_pending_tunnel_starts: dict[int, TunnelStartAttempt] = {}
_tunnel_generations: dict[str, int] = {}
# Restart setup can launch a long-lived resume/auto-sync coroutine after the
# replacement runner is registered. Keep those tasks owned by the runner so a
# stop/replacement can cancel them before they touch a disconnected RSD.
_tunnel_side_effects: dict[tuple[str, int | None], set[asyncio.Task]] = {}
_EXPECTED_CONNECTION_UNSET = object()
_EXPECTED_ENGINE_UNSET = object()


def _norm_udid(value: str | None) -> str | None:
    return value.casefold() if isinstance(value, str) else None


def _attempt_identities(attempt: TunnelStartAttempt) -> tuple[str, ...]:
    return tuple(
        value
        for value in (
            attempt.requested_udid,
            attempt.current_udid,
            attempt.resolved_udid,
            attempt.registry_key,
        )
        if value
    )


def _attempt_hard_identities(attempt: TunnelStartAttempt) -> tuple[str, ...]:
    """Identities whose stop watermark cancels the whole start route."""
    return tuple(
        value
        for value in (
            attempt.requested_udid,
            attempt.resolved_udid,
            attempt.registry_key,
        )
        if value
    )


def _start_attempt_fenced_locked(attempt: TunnelStartAttempt) -> bool:
    """Return whether a stop intent already fences this attempt.

    Caller must hold _tunnels_lock.  Keeping this predicate synchronous makes
    candidate commit and pending->actual re-key a single linearized decision.
    """
    if attempt.cancel_requested or attempt.sequence <= _tunnel_stop_all_watermark:
        return True
    return any(
        attempt.sequence <= _tunnel_stop_watermarks.get(_norm_udid(identity), -1)
        for identity in _attempt_hard_identities(attempt)
    )


def _candidate_start_fenced_locked(
    attempt: TunnelStartAttempt,
    candidate_udid: str,
) -> bool:
    """Return whether this candidate is fenced while other candidates may run."""
    if _start_attempt_fenced_locked(attempt):
        return True
    candidate = _norm_udid(candidate_udid)
    if candidate is None:
        return False
    # A per-UDID stop may have happened while a generic request was probing a
    # different candidate.  The attempt has no hard identity for that target,
    # but its older sequence is still fenced from probing the target later.
    if attempt.sequence <= _tunnel_stop_watermarks.get(candidate, -1):
        return True
    return candidate in {
        _norm_udid(value) for value in attempt.candidate_exclusions
    }


def _next_tunnel_generation_locked(udid: str) -> int:
    """Advance an identity generation; caller must hold _tunnels_lock."""
    generation = _tunnel_generations.get(udid, 0) + 1
    _tunnel_generations[udid] = generation
    return generation


def _tunnel_owner_locked(
    udid: str,
    runner: TunnelRunner,
    generation: int | None = None,
) -> bool:
    return (
        _tunnels.get(udid) is runner
        and (generation is None or _tunnel_generations.get(udid) == generation)
    )


def _attempt_matches_udid_locked(attempt: TunnelStartAttempt, udid: str) -> bool:
    wanted = _norm_udid(udid)
    return wanted is not None and wanted in {
        _norm_udid(identity) for identity in _attempt_identities(attempt)
    }


@asynccontextmanager
async def _acquire_tunnel_start_lane_until(
    ip: str | None,
    deadline: float | None,
    *,
    already_held: bool = False,
):
    """Acquire the normalized-IP start lane without exceeding ``deadline``.

    ``asyncio.wait_for`` cancels the lane waiter on timeout.  The lane keeps a
    reference for every waiter, so an idle IP entry can be removed only after
    both the owner and all cancelled/finished waiters have released it.
    """
    if already_held:
        yield
        return

    global _tunnel_start_lanes_loop, _tunnel_start_lanes
    loop = asyncio.get_running_loop()
    key = _normalize_tunnel_ip(ip)
    if _tunnel_start_lanes_loop is not loop:
        _tunnel_start_lanes_loop = loop
        _tunnel_start_lanes = {}
    lane = _tunnel_start_lanes.get(key)
    if lane is None:
        lane = _TunnelStartLane(asyncio.Lock())
        _tunnel_start_lanes[key] = lane
    lane.refs += 1
    acquired = False
    try:
        if deadline is None:
            await lane.lock.acquire()
        else:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TunnelStartTimedOut
            try:
                await asyncio.wait_for(lane.lock.acquire(), timeout=remaining)
            except asyncio.TimeoutError as exc:
                raise TunnelStartTimedOut from exc
        acquired = True
        yield
    finally:
        if acquired:
            lane.lock.release()
        # A different event loop may have become current while this context
        # was draining. Never mutate that loop's fresh lane map.
        if _tunnel_start_lanes_loop is loop and _tunnel_start_lanes.get(key) is lane:
            lane.refs -= 1
            if lane.refs <= 0:
                _tunnel_start_lanes.pop(key, None)


async def _spawn_owned_tunnel_side_effect(
    udid: str,
    runner: TunnelRunner,
    generation: int | None,
    factory,
) -> asyncio.Task | None:
    """Spawn a post-restart task only while this runner still owns ``udid``.

    The task remains associated with the runner until completion.  Detach and
    stop paths cancel the set outside ``_tunnels_lock`` so a late resume cannot
    clear an engine's stop event after the user has disconnected it.
    """
    async with _tunnels_lock:
        if not _tunnel_owner_locked(udid, runner, generation):
            return None
        task = asyncio.create_task(factory())
        owner_key = (udid, generation)
        _tunnel_side_effects.setdefault(owner_key, set()).add(task)

        def _forget(done: asyncio.Task) -> None:
            tasks = _tunnel_side_effects.get(owner_key)
            if tasks is not None:
                tasks.discard(done)
                if not tasks:
                    _tunnel_side_effects.pop(owner_key, None)
            try:
                error = done.exception()
            except asyncio.CancelledError:
                return
            if error is not None:
                _tunnel_logger.error(
                    "Owned tunnel side effect failed for %s generation=%s",
                    udid,
                    generation,
                    exc_info=(type(error), error, error.__traceback__),
                )

        task.add_done_callback(_forget)
        return task


def _take_tunnel_side_effects_locked(
    udid: str,
    *,
    generation: int | None = None,
) -> tuple[asyncio.Task, ...]:
    """Detach owned side-effect tasks; caller must hold ``_tunnels_lock``."""
    keys = [
        key for key in _tunnel_side_effects
        if key[0] == udid and (generation is None or key[1] == generation)
    ]
    return tuple(
        task
        for key in keys
        for task in _tunnel_side_effects.pop(key, set())
    )


async def _cancel_tunnel_side_effects(
    udid: str,
    *,
    generation: int | None = None,
) -> None:
    """Cancel owned post-restart tasks without awaiting under the registry lock.

    A generation filter lets an old restart clean up only its own side
    effects when a replacement has already won the registry race.
    """
    async with _tunnels_lock:
        tasks = _take_tunnel_side_effects_locked(
            udid,
            generation=generation,
        )
    await _stop_tunnel_parts(None, None, side_effects=tasks, caller="side_effect_cancel", udid=udid)


async def _register_tunnel_start(
    requested_udid: str | None,
    *,
    deadline: float | None = None,
) -> TunnelStartAttempt:
    """Register before a start waits on the serial network-resolution lock."""
    global _tunnel_start_sequence
    task = asyncio.current_task()
    async with _tunnels_lock:
        _tunnel_start_sequence += 1
        attempt = TunnelStartAttempt(
            _tunnel_start_sequence,
            requested_udid,
            task,
            deadline=deadline,
        )
        _pending_tunnel_starts[attempt.sequence] = attempt
    return attempt


async def _finish_tunnel_start_impl(
    attempt: TunnelStartAttempt,
    *,
    success: bool,
) -> None:
    """Forget an attempt and stop any runner it owns on an unsuccessful exit."""
    runner: TunnelRunner | None = None
    wd: asyncio.Task | None = None
    side_effects: tuple[asyncio.Task, ...] = ()
    async with _tunnels_lock:
        _pending_tunnel_starts.pop(attempt.sequence, None)
        if not success and attempt.runner_owned:
            runner = attempt.runner
            key = attempt.registry_key
            if key and _tunnels.get(key) is runner:
                _tunnels.pop(key, None)
                wd = _tunnel_watchdogs.pop(key, None)
                side_effects = _take_tunnel_side_effects_locked(key)
            elif not attempt.committed:
                # The handshake may have returned just before cancellation;
                # it is not in the registry yet but remains our runner.
                runner = attempt.runner
    if runner is not None or wd is not None or side_effects:
        await _stop_tunnel_parts(
            runner,
            wd,
            side_effects=side_effects,
            caller="tunnel_start_cancelled" if attempt.cancel_requested else "tunnel_start_failed",
            udid=attempt.resolved_udid or attempt.registry_key or attempt.current_udid or "<pending>",
        )


async def _finish_tunnel_start(
    attempt: TunnelStartAttempt,
    *,
    success: bool,
) -> None:
    """Finish an attempt even if its caller is being cancelled again."""
    cleanup = asyncio.create_task(
        _finish_tunnel_start_impl(attempt, success=success),
    )
    try:
        await asyncio.shield(cleanup)
    except asyncio.CancelledError:
        await _await_cleanup_task(cleanup)
        raise


async def _request_tunnel_stop(
    target_udid: str | None,
    *,
    dm=None,
) -> TunnelStopPlan:
    """Linearize stop intent, registry detach, and exact DM lease snapshots."""
    global _tunnel_stop_all_watermark
    tasks: list[asyncio.Task] = []
    candidate_parts: list[TunnelStopPart] = []
    udids: list[str] = []
    seen_udids: set[str] = set()
    matched_pending = False
    if dm is None:
        dm = _dm()
    try:
        from main import app_state
    except Exception:
        app_state = None

    def _add_udid(value: str) -> None:
        if value not in seen_udids:
            seen_udids.add(value)
            udids.append(value)

    async with _tunnels_lock:
        watermark = _tunnel_start_sequence
        hard_identity_targets: set[str] = set()
        registry_keys: list[str] = []
        network_keys: list[str] = [
            key
            for key, conn in dm._connections.items()
            if getattr(conn, "connection_type", "") == "Network"
        ]
        if target_udid is None:
            _tunnel_stop_all_watermark = max(_tunnel_stop_all_watermark, watermark)
            matches = [
                attempt
                for attempt in _pending_tunnel_starts.values()
                if attempt.sequence <= watermark
            ]
            registry_keys = list(_tunnels.keys())
            hard_identity_targets.update(_norm_udid(key) for key in registry_keys)
            hard_identity_targets.update(_norm_udid(key) for key in network_keys)
        else:
            key = _norm_udid(target_udid)
            if key is not None:
                _tunnel_stop_watermarks[key] = max(
                    _tunnel_stop_watermarks.get(key, 0), watermark,
                )
            matches = [
                attempt
                for attempt in _pending_tunnel_starts.values()
                if attempt.sequence <= watermark
                and (
                    key in {
                        _norm_udid(identity)
                        for identity in _attempt_hard_identities(attempt)
                    }
                    or _norm_udid(attempt.current_udid) == key
                )
            ]

        for attempt in matches:
            matched_pending = True
            hard_match = target_udid is None or any(
                _norm_udid(identity) in {
                    _norm_udid(target_udid),
                }
                for identity in _attempt_hard_identities(attempt)
            )
            if hard_match:
                attempt.cancel_requested = True
                if attempt.task is not None and attempt.task is not asyncio.current_task():
                    tasks.append(attempt.task)
                for identity in _attempt_hard_identities(attempt):
                    hard_identity_targets.add(_norm_udid(identity))
                    _add_udid(identity)
            elif target_udid is not None:
                # A stop aimed at only the active generic candidate must stop
                # that probe, then let this request try other pair records.
                candidate = _norm_udid(attempt.current_udid)
                if candidate is not None:
                    attempt.candidate_exclusions.add(candidate)
                    _add_udid(attempt.current_udid or candidate)
                    # Cancel only the child handshake for the active
                    # candidate.  The parent start route is deliberately not
                    # cancelled: it must continue with unrelated fallback
                    # pair records.  Once the child is cancelled, its own
                    # start/finally path drains the runner.  If no probe is
                    # in flight (the runner is between handshake and commit),
                    # detach/stop that runner as a standalone candidate part.
                    if (
                        attempt.probe_task is not None
                        and not attempt.probe_task.done()
                    ):
                        attempt.probe_task.cancel()
                    elif attempt.runner is not None:
                        candidate_parts.append(
                            TunnelStopPart(
                                udid=attempt.current_udid or candidate,
                                runner=attempt.runner,
                                expected_connection=None,
                                expected_engine=None,
                            )
                        )

            for identity in _attempt_identities(attempt):
                if target_udid is None or _norm_udid(identity) in hard_identity_targets:
                    _add_udid(identity)

        if target_udid is not None:
            registry_keys = [
                key
                for key in _tunnels
                if _norm_udid(key) in hard_identity_targets
                or _norm_udid(key) == _norm_udid(target_udid)
            ]
            network_keys = [
                key
                for key in network_keys
                if _norm_udid(key) in hard_identity_targets
                or _norm_udid(key) == _norm_udid(target_udid)
            ]

        parts: list[TunnelStopPart] = []
        part_keys: set[str] = set()
        for key in registry_keys:
            runner = _tunnels.pop(key, None)
            watchdog = _tunnel_watchdogs.pop(key, None)
            generation = _tunnel_generations.get(key)
            side_effects = _take_tunnel_side_effects_locked(key)
            if runner is not None:
                _next_tunnel_generation_locked(key)
            conn = dm._connections.get(key)
            expected = (
                conn
                if conn is not None and getattr(conn, "connection_type", "") == "Network"
                else None
            )
            expected_engine = (
                app_state.simulation_engines.get(key)
                if app_state is not None
                else None
            )
            parts.append(
                TunnelStopPart(
                    udid=key,
                    runner=runner,
                    watchdog=watchdog,
                    side_effects=side_effects,
                    expected_connection=expected,
                    expected_engine=expected_engine,
                    generation=generation,
                )
            )
            part_keys.add(key)
            _add_udid(key)

        # A Network connection may exist without a visible tunnel registry
        # entry (e.g. start-and-connect is between dm.connect and re-key).
        for key in network_keys:
            if key in part_keys:
                continue
            conn = dm._connections.get(key)
            if conn is None or getattr(conn, "connection_type", "") != "Network":
                continue
            expected_engine = (
                app_state.simulation_engines.get(key)
                if app_state is not None
                else None
            )
            parts.append(
                TunnelStopPart(
                    udid=key,
                    expected_connection=conn,
                    expected_engine=expected_engine,
                )
            )
            part_keys.add(key)
            _add_udid(key)

        # Include detached candidate probes after registry parts. A runner
        # that became registered above is already owned by that part.
        # TunnelRunner is intentionally mutable (and test doubles may be
        # unhashable), so compare object identity rather than putting runners
        # themselves in a set.
        registered_runner_ids = {
            id(part.runner) for part in parts if part.runner is not None
        }
        for part in candidate_parts:
            if part.runner is not None and id(part.runner) in registered_runner_ids:
                continue
            parts.append(part)
            _add_udid(part.udid)

    # Cancellation is deliberately outside the registry lock. The plan owns
    # all detached lifecycle objects; pending start tasks own their runner
    # cleanup after observing cancel_requested.
    for task in tasks:
        if not task.done():
            task.cancel()
    return TunnelStopPlan(
        target_udid=target_udid,
        udids=udids,
        parts=parts,
        pending_tasks=tuple(tasks),
        matched_pending=matched_pending,
    )


async def _cleanup_wifi_connection_for(
    udid: str,
    *,
    caller: str,
    expected_connection: object = _EXPECTED_CONNECTION_UNSET,
    expected_engine: object = _EXPECTED_ENGINE_UNSET,
    broadcast: bool = True,
) -> bool:
    """Disconnect a single WiFi-connected device + drop its sim engine.
    Broadcasts device_disconnected for that udid so the frontend chip
    flips to disconnected. Returns True iff a Network connection was found
    and torn down.

    The engine MUST be stopped before the dm.disconnect closes the RSD —
    otherwise its in-flight task (random_walk / loop / multi_stop /
    navigate / _move_along_route) keeps trying to push positions through
    the now-dead RSD, hits DeviceLostError, retries, fails again, and
    floods the log with `Giving up on this route after repeated push
    failures` every ~2 seconds for as long as LocWarp stays open.
    Mirrors the USB watchdog teardown sequence in main.py:387-418."""
    dm = _dm()
    try:
        from main import app_state
    except Exception:
        app_state = None
    engine_requested = expected_engine is not _EXPECTED_ENGINE_UNSET
    if not engine_requested:
        expected_engine = (
            app_state.simulation_engines.get(udid)
            if app_state is not None
            else None
        )
        engine_requested = True

    current = dm._connections.get(udid)
    # ``None`` is an explicit no-op sentinel, not an implicit snapshot.  A
    # detached engine-only plan must never pop whichever newer C2 lease is
    # currently in the manager.
    connection_requested = (
        expected_connection is not _EXPECTED_CONNECTION_UNSET
        and expected_connection is not None
    )
    if expected_connection is _EXPECTED_CONNECTION_UNSET:
        if current is None or getattr(current, "connection_type", "") != "Network":
            if expected_engine is None:
                return False
            # A detached runner can still own an old engine after a newer
            # connection replaced/removed the DM lease.  Stop that exact
            # engine even though there is no connection to close.
            conn = None
        else:
            expected_connection = current
            connection_requested = True
            conn = None
    elif expected_connection is None:
        # Explicit ``None`` means engine-only cleanup.  In particular, a
        # watchdog without a lease must never snapshot/detach a newer C2.
        if expected_engine is None:
            return False
        conn = None
    elif getattr(expected_connection, "connection_type", "") != "Network":
        if expected_engine is None:
            return False
        conn = None

    if connection_requested:
        detach = getattr(dm, "_detach_connection", None)
        if detach is not None:
            conn = await detach(udid, expected=expected_connection)
        else:
            # Keep lightweight test doubles and older integrations usable
            # while production DeviceManager supplies identity-CAS.
            if dm._connections.get(udid) is expected_connection:
                conn = dm._connections.pop(udid, None)
            else:
                conn = None
        # A newer lease may have won after the stop plan snapshot.  Do not
        # close or broadcast for it, but continue below to stop the exact old
        # engine captured by the plan.
        connection_detached = conn is expected_connection
    else:
        connection_detached = False

    async def _cleanup_detached() -> None:
        # Stop the running simulation BEFORE we close the underlying lockdown,
        # so its retry loop doesn't get a chance to log a dozen DeviceLostError
        # rounds against the dying RSD.
        old_eng = expected_engine
        if old_eng is not None:
            try:
                from models.schemas import SimulationState as _SS
                old_eng.state = _SS.DISCONNECTED
                stop = getattr(old_eng, "stop", None)
                stop_failed = False
                if callable(stop):
                    try:
                        result = stop()
                        if inspect.isawaitable(result):
                            await result
                    except asyncio.CancelledError:
                        stop_failed = True
                        _tunnel_logger.debug(
                            "[%s] old engine stop cancelled for %s",
                            caller,
                            udid,
                            exc_info=True,
                        )
                    except Exception:
                        stop_failed = True
                        _tunnel_logger.debug(
                            "[%s] old engine stop failed for %s",
                            caller,
                            udid,
                            exc_info=True,
                        )
                if not callable(stop) or stop_failed:
                    # Lightweight test doubles and older engine adapters may
                    # not expose stop().  Quiesce their exact task explicitly
                    # and drain it before the RSD connection is closed.
                    old_eng._stop_event.set()
                    old_eng._pause_event.set()
                    active = getattr(old_eng, "_active_task", None)
                    if active is not None and not active.done():
                        active.cancel()
                    if active is not None:
                        await asyncio.gather(active, return_exceptions=True)
                try:
                    # Defensive doubles may reset state while stopping; the
                    # lifecycle event must still describe the disconnected
                    # engine that was just quiesced.
                    old_eng.state = _SS.DISCONNECTED
                    emit = getattr(old_eng, "_emit", None)
                    if callable(emit):
                        state = getattr(old_eng.state, "value", old_eng.state)
                        await emit("state_change", {"state": state})
                except BaseException:
                    _tunnel_logger.debug(
                        "[%s] disconnected state_change emit failed for %s",
                        caller,
                        udid,
                        exc_info=True,
                    )
            except Exception:
                _tunnel_logger.debug(
                    "[%s] failed to stop old engine for %s", caller, udid, exc_info=True,
                )

        if connection_detached:
            close_detached = getattr(dm, "_close_detached_connection", None)
            drain_detached = getattr(dm, "_drain_detached_close", None)
            try:
                if drain_detached is not None:
                    await drain_detached(udid, conn)
                elif close_detached is not None:
                    await close_detached(udid, conn)
                else:
                    await dm.disconnect(udid, expected=conn)
                _tunnel_logger.info("[%s] Disconnected WiFi device %s", caller, udid)
            except (OSError, RuntimeError):
                _tunnel_logger.exception("[%s] Failed to disconnect %s", caller, udid)
        if app_state.simulation_engines.get(udid) is old_eng:
            app_state.simulation_engines.pop(udid, None)
        if app_state._primary_udid == udid and udid not in app_state.simulation_engines:
            remaining = next(iter(app_state.simulation_engines.keys()), None)
            app_state._primary_udid = remaining
        async def _broadcast_if_still_detached() -> None:
            # The final C/E absence decision and the notification must be one
            # serialized transaction in production.  A concurrent USB C2
            # install otherwise can land between this check and the event,
            # making a healthy replacement look disconnected to the UI.
            if not connection_detached or not broadcast:
                return
            replacement = dm._connections.get(udid)
            engine_replacement = app_state.simulation_engines.get(udid)
            if replacement is not None or engine_replacement is not None:
                return
            try:
                from api.websocket import broadcast as _broadcast
                await _broadcast("device_disconnected", {
                    "udid": udid,
                    "udids": [udid],
                    "reason": "wifi_tunnel_stopped",
                    "remaining_count": len(dm._connections),
                })
            except Exception:
                _tunnel_logger.exception("[%s] WiFi cleanup broadcast failed", caller)

        dm_lock = getattr(dm, "_lock", None)
        if dm_lock is None:
            # Lightweight test doubles and older integrations may not expose
            # DeviceManager's lifecycle lock; preserve their prior behavior.
            await _broadcast_if_still_detached()
        else:
            async with dm_lock:
                await _broadcast_if_still_detached()

    cleanup = asyncio.create_task(_cleanup_detached())
    caller_cancelled, error = await _await_cleanup_task(cleanup)
    if error is not None:
        raise error
    if caller_cancelled:
        raise asyncio.CancelledError
    return connection_detached


async def _cleanup_all_wifi_connections(caller: str = "unknown") -> list[str]:
    """Disconnect every Network device + drop their sim engines. Used by
    the legacy stop-all flow and shutdown paths."""
    import traceback
    dm = _dm()
    from main import app_state
    stack = traceback.extract_stack(limit=8)[:-1]
    stack_str = " <- ".join(f"{fr.name}@{fr.filename.split(chr(92))[-1]}:{fr.lineno}" for fr in reversed(stack))
    _tunnel_logger.warning(
        "_cleanup_all_wifi_connections called (caller=%s); stack: %s",
        caller, stack_str,
    )
    leases = [
        (
            udid,
            conn,
            getattr(app_state, "simulation_engines", {}).get(udid),
        )
        for udid, conn in list(dm._connections.items())
        if getattr(conn, "connection_type", "") == "Network"
    ]
    for udid, lease, engine in leases:
        await _cleanup_wifi_connection_for(
            udid,
            expected_connection=lease,
            expected_engine=engine,
            caller=caller,
        )
    return [udid for udid, _lease, _engine in leases]


async def _detach_tunnel(
    udid: str,
    *,
    expected: TunnelRunner | None = None,
) -> tuple[TunnelRunner | None, asyncio.Task | None, tuple[asyncio.Task, ...]]:
    """Atomically remove one registry entry without awaiting lifecycle work."""
    async with _tunnels_lock:
        current = _tunnels.get(udid)
        if expected is not None and current is not expected:
            return None, None, ()
        runner = _tunnels.pop(udid, None)
        wd = _tunnel_watchdogs.pop(udid, None)
        side_effects = _take_tunnel_side_effects_locked(udid)
        if runner is not None:
            _next_tunnel_generation_locked(udid)
    return runner, wd, side_effects


async def _await_cleanup_task(awaitable) -> tuple[bool, BaseException | None]:
    """Drain cleanup despite caller cancellation, then report it to caller."""
    drain = asyncio.ensure_future(
        asyncio.gather(awaitable, return_exceptions=True),
    )
    caller_cancelled = False
    current = asyncio.current_task()
    while True:
        if current is not None and current.cancelling():
            caller_cancelled = True
        try:
            await asyncio.shield(drain)
        except asyncio.CancelledError:
            # Shield keeps the child cleanup alive.  A repeated cancellation
            # is remembered and re-raised by the caller after cleanup.
            caller_cancelled = True
            continue
        if drain.done():
            # A stop/cancel scheduled in the same loop turn as child
            # completion must still be observed before returning.
            if current is not None and current.cancelling():
                caller_cancelled = True
            break
    result = drain.result()[0]
    if isinstance(result, BaseException):
        return caller_cancelled, result
    return caller_cancelled, None


async def _cancel_cleanup_tasks(
    tasks: tuple[asyncio.Task, ...],
    *,
    current_task: asyncio.Task | None,
) -> None:
    """Cancel and retrieve detached side-effect tasks."""
    pending = []
    for task in tasks:
        if task is current_task:
            continue
        if not task.done():
            task.cancel()
        pending.append(task)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


async def _stop_tunnel_parts(
    runner: TunnelRunner | None,
    wd: asyncio.Task | None,
    *,
    caller: str,
    udid: str = "<detached>",
    side_effects: tuple[asyncio.Task, ...] = (),
) -> None:
    """Stop detached side-effects/watchdog/runner outside registry locks."""
    caller_cancelled = False
    current_task = asyncio.current_task()
    if side_effects:
        cancelled, _error = await _await_cleanup_task(
            _cancel_cleanup_tasks(side_effects, current_task=current_task),
        )
        caller_cancelled = caller_cancelled or cancelled
    if wd is not None:
        if wd is current_task:
            # A watchdog may tear down its own registry slot.  Never cancel
            # the current task while it is still responsible for finishing
            # the runner/lease cleanup below.
            pass
        elif not wd.done():
            wd.cancel()
            cancelled, _error = await _await_cleanup_task(wd)
            caller_cancelled = caller_cancelled or cancelled
    if runner is not None:
        cancelled, error = await _await_cleanup_task(runner.stop())
        caller_cancelled = caller_cancelled or cancelled
        if error is not None and not isinstance(error, asyncio.CancelledError):
            _tunnel_logger.error(
                "[%s] runner.stop failed for %s",
                caller,
                udid,
                exc_info=(type(error), error, error.__traceback__),
            )
    if caller_cancelled:
        raise asyncio.CancelledError


async def _stop_tunnel_plan_part(
    part: TunnelStopPart,
    *,
    caller: str,
) -> bool:
    """Apply one linearized stop part in side-effect/WD/lease/runner order."""
    await _stop_tunnel_parts(
        None,
        part.watchdog,
        side_effects=part.side_effects,
        caller=caller,
        udid=part.udid,
    )
    cleaned = False
    if part.expected_connection is not None:
        cleaned = await _cleanup_wifi_connection_for(
            part.udid,
            expected_connection=part.expected_connection,
            expected_engine=part.expected_engine,
            caller=caller,
        )
    elif part.expected_engine is not None:
        await _cleanup_wifi_connection_for(
            part.udid,
            # This stop plan owns only the detached engine.  Passing the
            # explicit None sentinel is essential: omitting it would let the
            # helper snapshot and detach a newer Network lease (C2) that was
            # installed after the old plan was built.
            expected_connection=None,
            expected_engine=part.expected_engine,
            caller=caller,
        )
    await _stop_tunnel_parts(
        part.runner,
        None,
        caller=caller,
        udid=part.udid,
    )
    return cleaned


async def _stop_tunnel_plan(
    plan: TunnelStopPlan,
    *,
    caller: str,
) -> bool:
    """Apply a detached stop plan and drain matched pending start tasks.

    The route owns the request task, so an external cancellation must not
    interrupt the side-effect/watchdog/lease/runner sequence halfway through.
    Run the plan in a child task, shield it, and re-raise cancellation only
    after every detached part and pending start has been retrieved.
    """
    async def _apply() -> bool:
        cleaned = False
        for part in plan.parts:
            cleaned = await _stop_tunnel_plan_part(part, caller=caller) or cleaned
        pending = tuple(
            task for task in plan.pending_tasks
            if task is not asyncio.current_task()
        )
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        return cleaned

    cleanup = asyncio.create_task(_apply())
    try:
        return await asyncio.shield(cleanup)
    except asyncio.CancelledError:
        # Repeated cancellation must not cut the detached plan short.  The
        # shared drain helper keeps waiting for the child and only then lets
        # this route propagate its caller cancellation.
        await _await_cleanup_task(cleanup)
        raise


async def _tear_down_tunnel(udid: str, *, caller: str) -> None:
    """Detach this udid, then stop its watchdog/runner without holding locks."""
    runner, wd, side_effects = await _detach_tunnel(udid)
    await _stop_tunnel_parts(
        runner,
        wd,
        side_effects=side_effects,
        caller=caller,
        udid=udid,
    )


# Restart backoff sequence (seconds). Three attempts cover most WiFi blips
# (transient packet loss, brief screen-lock pause) without sitting on a dead
# tunnel for an unbounded time. Total worst-case wait ~21s before final
# teardown — within the user's tolerance for "auto-recovers" before they'd
# look at the UI and notice.
_TUNNEL_RESTART_BACKOFF: tuple[float, ...] = (3.0, 6.0, 12.0)


async def _attempt_tunnel_restart_impl(
    udid: str,
    ip: str,
    port: int,
    snapshot: dict | None,
    original_runner: TunnelRunner,
    connection_lease: object | None = None,
    *,
    adoption_lock: asyncio.Lock | None = None,
) -> bool:
    """Try one restart of the tunnel. On success, swaps in the new runner,
    rebuilds dm._connections + sim engine (since the new RSD interface gets
    a fresh address), and resumes any captured snapshot. Returns True on
    success, False otherwise. Caller decides whether to retry."""
    new_runner = TunnelRunner()
    try:
        info = await new_runner.start(udid, ip, port, timeout=10.0)
    except asyncio.CancelledError:
        await _stop_tunnel_parts(
            new_runner,
            None,
            caller="watchdog_restart_start_cancelled",
            udid=udid,
        )
        raise
    except Exception as exc:
        _tunnel_logger.warning(
            "Tunnel restart failed for %s: %s: %s",
            udid, type(exc).__name__, exc,
        )
        await _stop_tunnel_parts(
            new_runner,
            None,
            caller="watchdog_restart_start_failed",
            udid=udid,
        )
        return False

    new_rsd_address = info.get("rsd_address")
    new_rsd_port = info.get("rsd_port")
    if not new_rsd_address or not new_rsd_port:
        _tunnel_logger.warning(
            "Tunnel restart for %s returned no RSD info; treating as failure",
            udid,
        )
        await _stop_tunnel_parts(
            new_runner,
            None,
            caller="watchdog_restart_no_rsd",
            udid=udid,
        )
        return False

    # Runner.start above is deliberately outside this lock.  The lock begins
    # only at the adoption commit and remains held through its exact rollback.
    adoption_lock = adoption_lock or _get_tunnel_lifecycle_lock()
    adoption_lock_acquired = False
    try:
        await adoption_lock.acquire()
        adoption_lock_acquired = True
    except BaseException:
        await _stop_tunnel_parts(
            new_runner,
            None,
            caller="watchdog_restart_adoption_cancelled",
            udid=udid,
        )
        raise

    from main import app_state
    connected_udid: str | None = None
    connected_lease: object | None = None
    connected_engine: object | None = None
    was_primary = False
    restart_generation: int | None = None
    original_generation: int | None = None

    async def _restore_or_detach_restart() -> tuple[
        bool,
        asyncio.Task | None,
        tuple[asyncio.Task, ...],
        bool,
        bool,
    ]:
        """CAS-restore this restart, or detach its exact registry lease.

        Registry ownership must be resolved before stopping ``new_runner``.
        Otherwise a conflict/rollback can leave a stopped runner visible under
        the replacement generation, or resurrect an entry replaced by stop.
        """
        restored_original = False
        detached_wd: asyncio.Task | None = None
        detached_side_effects: tuple[asyncio.Task, ...] = ()
        replacement_won = False
        same_runner_rearmed = False
        async with _tunnels_lock:
            if (
                restart_generation is not None
                and _tunnel_owner_locked(udid, new_runner, restart_generation)
            ):
                _tunnels[udid] = original_runner
                _tunnel_generations[udid] = original_generation or 0
                restored_original = True
            elif (
                restart_generation is not None
                and _tunnels.get(udid) is new_runner
                and _tunnel_generations.get(udid) == restart_generation
            ):
                # Detach only the exact Rnew/Gnew lease. Never touch a
                # replacement runner that won the registry race.
                _tunnels.pop(udid, None)
                detached_wd = _tunnel_watchdogs.pop(udid, None)
                detached_side_effects = _take_tunnel_side_effects_locked(
                    udid,
                    generation=restart_generation,
                )
                _next_tunnel_generation_locked(udid)
            else:
                replacement_won = _tunnels.get(udid) is not None
                same_runner_rearmed = (
                    _tunnels.get(udid) is new_runner
                    and _tunnel_generations.get(udid) != restart_generation
                )
        return (
            restored_original,
            detached_wd,
            detached_side_effects,
            replacement_won,
            same_runner_rearmed,
        )

    try:
        # User may have stopped this tunnel during our async window. Keep the
        # registry mutation short, then stop a losing runner after releasing
        # the lock; TunnelRunner.stop() can wait on network I/O.
        discard_restart = False
        try:
            async with _tunnels_lock:
                if _tunnels.get(udid) is not original_runner:
                    discard_restart = True
                else:
                    original_generation = _tunnel_generations.get(udid, 0)
                    restart_generation = _next_tunnel_generation_locked(udid)
                    _tunnels[udid] = new_runner
        except asyncio.CancelledError:
            # The restart runner is not registered yet; cancellation at the
            # lock boundary must still retrieve and stop its owned task.
            await _stop_tunnel_parts(
                new_runner, None,
                caller="watchdog_restart_cancelled", udid=udid,
            )
            raise
        if discard_restart:
            _tunnel_logger.info(
                "Tunnel restart for %s racing user stop; discarding new runner",
                udid,
            )
            await _stop_tunnel_parts(
                new_runner, None, caller="watchdog_restart_race", udid=udid,
            )
            # The old watchdog no longer owns this candidate.  Report a
            # failed attempt; the caller re-checks ownership before any next
            # candidate and therefore exits without retrying a stale owner.
            return False

        # connect_wifi_tunnel internally calls disconnect(udid) if udid
        # already exists, so the old (now-dead) RSD lockdown gets torn
        # down correctly.
        dm = _dm()

        async def _before_close_previous(
            previous_udid: str,
            _previous,
        ) -> None:
            """Quiesce exact E0 before DeviceManager installs C1."""
            nonlocal was_primary
            was_primary = app_state._primary_udid == previous_udid
            stale_engine = app_state.simulation_engines.get(previous_udid)
            if stale_engine is not None:
                await _cleanup_wifi_connection_for(
                    previous_udid,
                    expected_connection=None,
                    expected_engine=stale_engine,
                    caller="watchdog_restart_engine_rebuild",
                    broadcast=False,
                )

        if (
            connection_lease is not None
            and dm._connections.get(udid) is not None
            and dm._connections.get(udid) is not connection_lease
        ):
            # A newer DM lease already won while this generation was asleep;
            # never let the stale watchdog replace or close it. Restore the
            # prior runner generation while Rnew still owns the registry so a
            # stopped Rnew can never remain registered.
            (
                restored_original,
                detached_wd,
                detached_side_effects,
                replacement_won,
                same_runner_rearmed,
            ) = (
                await _restore_or_detach_restart()
            )
            if restored_original:
                await _cancel_tunnel_side_effects(
                    udid,
                    generation=restart_generation,
                )
            else:
                await _stop_tunnel_parts(
                    None,
                    detached_wd,
                    side_effects=detached_side_effects,
                    caller="watchdog_restart_stale_connection",
                    udid=udid,
                )
            if not same_runner_rearmed:
                await _stop_tunnel_parts(
                    new_runner,
                    None,
                    caller="watchdog_restart_stale_connection",
                    udid=udid,
                )
            return False
        dev_info, connected_lease = await dm.connect_wifi_tunnel_owned(
            new_rsd_address,
            new_rsd_port,
            before_close_previous=_before_close_previous,
        )
        connected_udid = dev_info.udid

        # Discovery-assisted fallback can hand us an endpoint that turned
        # out to be a DIFFERENT iPhone (family device on the same LAN).
        # The pair-record handshake usually rejects that first, but if it
        # does connect, verify identity and bail — the except path below
        # rolls back the runner we registered. Compare case-insensitively:
        # dev_info.udid comes from the RSD peer_info while `udid` comes
        # from the pair-record filename / request, and those two sources
        # don't always agree on case (see main.py's connected_original /
        # present_usb_original normalization for the same issue). Skip the
        # check entirely for the "pending:ip:port" sentinel key used before
        # a real udid is known — it can never equal a real udid anyway.
        if not udid.startswith("pending:") and dev_info.udid.lower() != udid.lower():
            _tunnel_logger.warning(
                "Tunnel restart for %s reached a different device (%s); disconnecting",
                udid, dev_info.udid,
            )
            try:
                await _cleanup_wifi_connection_for(
                    dev_info.udid,
                    expected_connection=connected_lease,
                    expected_engine=None,
                    caller="watchdog_restart_identity_mismatch",
                    broadcast=False,
                )
            except Exception:
                _tunnel_logger.debug(
                    "Disconnect of mismatched device failed", exc_info=True,
                )
            raise RuntimeError(
                f"udid mismatch: expected {udid}, got {dev_info.udid}"
            )

        async def _restart_still_owned() -> bool:
            async with _tunnels_lock:
                return _tunnel_owner_locked(udid, new_runner, restart_generation)

        async def _discard_owned_restart(caller: str) -> bool:
            await _cancel_tunnel_side_effects(
                udid,
                generation=restart_generation,
            )
            async with _tunnels_lock:
                current_runner = _tunnels.get(udid)
                current_generation = _tunnel_generations.get(udid)
                replacement_won = (
                    current_runner is not None
                    and (
                        current_runner is not new_runner
                        or current_generation != restart_generation
                    )
                )
                same_runner_rearmed = (
                    current_runner is new_runner
                    and current_generation != restart_generation
                )
            # Once registry ownership is lost, clean up only the exact lease
            # this restart acquired. The DM identity CAS preserves a newer
            # C2/E2, while still draining C1/E1 when no replacement won.
            if connected_udid and connected_lease is not None:
                await _cleanup_wifi_connection_for(
                    connected_udid,
                    expected_connection=connected_lease,
                    expected_engine=(
                        None if replacement_won else connected_engine
                    ),
                    caller=caller,
                    broadcast=False,
                )
            elif (
                not replacement_won
                and connected_udid
                and connected_engine is not None
            ):
                await _cleanup_wifi_connection_for(
                    connected_udid,
                    expected_connection=None,
                    expected_engine=connected_engine,
                    caller=caller,
                    broadcast=False,
                )
            if not same_runner_rearmed:
                await _stop_tunnel_parts(
                    new_runner,
                    None,
                    caller=caller,
                    udid=udid,
                )
            # Preserve the historical truthy discard result for runner-only
            # probes, while a stale lease-bearing generation reports failure
            # so its caller does not treat it as a successful restart.
            return connected_lease is None

        if not await _restart_still_owned():
            return await _discard_owned_restart("watchdog_restart_lost_ownership")

        # Rebuild the sim engine bound to the new location service. The
        # old engine pointed at the dead RSD and would throw
        # ConnectionTerminatedError on the next teleport / position push.
        # create_engine_for_device may install a new engine before a later
        # setup step raises.  Capture the identity in a finally block so the
        # rollback can stop/remove exactly that Enew, never the restored
        # generation's engine.
        try:
            await app_state.create_engine_for_device(dev_info.udid)
        finally:
            connected_engine = app_state.simulation_engines.get(dev_info.udid)
        if (
            was_primary
            and connected_engine is app_state.simulation_engines.get(dev_info.udid)
        ):
            app_state._primary_udid = dev_info.udid

        # Keep the original watchdog as the owner until all post-setup side
        # effects finish. A stop can therefore invalidate this task before a
        # resume or broadcast, instead of letting an old task announce a
        # tunnel that the user already stopped.
        if not await _restart_still_owned():
            return await _discard_owned_restart("watchdog_restart_lost_ownership")

        # Resume any in-flight simulation (navigate / loop / multi-stop /
        # random_walk) so the iPhone keeps moving instead of stopping at
        # the blip point. Snapshot only exists when this device was the
        # one driving the sim — followers don't capture one.
        if snapshot is not None:
            new_eng = app_state.simulation_engines.get(dev_info.udid)
            if new_eng is not None:
                _tunnel_logger.info(
                    "Resuming sim from snapshot after tunnel restart for %s (kind=%s)",
                    dev_info.udid, snapshot.get("kind"),
                )
                await _spawn_owned_tunnel_side_effect(
                    udid,
                    new_runner,
                    restart_generation,
                    lambda: new_eng.resume_from_snapshot(snapshot),
                )
        else:
            # Group-mode: if this WiFi device was a follower of some other
            # primary (USB or WiFi), restart broke the follower task. Re-
            # arm the same teleport-to-primary + attach-as-follower flow
            # the USB watchdog uses for re-plugged USB devices, so dual /
            # triple-device groups stay locked together across a blip.
            try:
                from main import _auto_sync_new_device_to_primary
                await _spawn_owned_tunnel_side_effect(
                    udid,
                    new_runner,
                    restart_generation,
                    lambda: _auto_sync_new_device_to_primary(dev_info.udid),
                )
            except Exception:
                _tunnel_logger.exception(
                    "Auto-sync after tunnel restart failed for %s", dev_info.udid,
                )

        if not await _restart_still_owned():
            return await _discard_owned_restart("watchdog_restart_lost_ownership")

        try:
            from api.websocket import broadcast
            if not await _restart_still_owned():
                return await _discard_owned_restart("watchdog_restart_lost_ownership")
            await broadcast("tunnel_recovered", {
                "udid": dev_info.udid,
                "rsd_address": new_rsd_address,
                "rsd_port": new_rsd_port,
                "ip": ip,
                "port": port,
            })
            if not await _restart_still_owned():
                return await _discard_owned_restart("watchdog_restart_lost_ownership")
            await broadcast("device_connected", {
                "udid": dev_info.udid,
                "name": dev_info.name,
                "ios_version": dev_info.ios_version,
                "connection_type": "Network",
            })
        except Exception:
            _tunnel_logger.exception("Failed to broadcast tunnel_recovered for %s", udid)

        # Linearize re-arm after every old-owner side effect. The new task is
        # given the generation token so a later detach invalidates it before
        # it can perform any recovery work of its own.
        restart_lost_ownership = False
        async with _tunnels_lock:
            if not _tunnel_owner_locked(udid, new_runner, restart_generation):
                restart_lost_ownership = True
            else:
                _tunnel_watchdogs[udid] = asyncio.create_task(
                    _per_tunnel_watchdog(
                        udid,
                        new_runner,
                        restart_generation,
                        connection_lease=connected_lease,
                        connection_engine=app_state.simulation_engines.get(
                            dev_info.udid,
                        ),
                    )
                )
        if restart_lost_ownership:
            return await _discard_owned_restart("watchdog_restart_lost_ownership")

        _tunnel_logger.info(
            "Tunnel restart succeeded for %s (rsd %s:%d)",
            udid, new_rsd_address, new_rsd_port,
        )
        return True
    except BaseException as exc:
        _tunnel_logger.exception(
            "Tunnel restart for %s started but post-setup failed; rolling back",
            udid,
        )
        # Roll back the new runner we registered, restoring the caller's
        # original runner rather than popping the slot empty. An empty
        # slot would make the "_tunnels.get(udid) is not runner" guards in
        # the watchdog's retry/fallback loops misread this as a user-
        # initiated stop and return early — skipping remaining fallback
        # candidates and the tunnel_lost teardown that's supposed to run
        # once every candidate is exhausted.
        async def _rollback() -> None:
            (
                restored_original,
                detached_wd,
                detached_side_effects,
                replacement_won,
                same_runner_rearmed,
            ) = (
                await _restore_or_detach_restart()
            )
            if restored_original:
                await _cancel_tunnel_side_effects(
                    udid,
                    generation=restart_generation,
                )
            else:
                await _stop_tunnel_parts(
                    None,
                    detached_wd,
                    side_effects=detached_side_effects,
                    caller="watchdog_restart_rollback",
                    udid=udid,
                )
            if (
                not replacement_won
                and connected_udid
                and connected_lease is not None
            ):
                # Exact C/E CAS keeps a concurrent replacement untouched.
                try:
                    await _cleanup_wifi_connection_for(
                        connected_udid,
                        expected_connection=connected_lease,
                        expected_engine=connected_engine,
                        caller="watchdog_restart_rollback",
                        broadcast=False,
                    )
                except Exception:
                    _tunnel_logger.exception(
                        "Failed to clean up rolled-back Network connection %s",
                        connected_udid,
                    )
            if not same_runner_rearmed:
                await _stop_tunnel_parts(
                    new_runner,
                    None,
                    caller="watchdog_restart_rollback",
                    udid=udid,
                )

        # CancelledError is a BaseException and can arrive after Cnew/Enew
        # installation. Drain the exact rollback before re-raising it.
        rollback = asyncio.create_task(_rollback())
        _cancelled, rollback_error = await _await_cleanup_task(rollback)
        if rollback_error is not None:
            _tunnel_logger.error(
                "Tunnel restart rollback failed for %s",
                udid,
                exc_info=(
                    type(rollback_error),
                    rollback_error,
                    rollback_error.__traceback__,
                ),
            )
        if isinstance(exc, asyncio.CancelledError):
            raise
        return False
    finally:
        if adoption_lock_acquired:
            adoption_lock.release()


async def _attempt_tunnel_restart(
    udid: str,
    ip: str,
    port: int,
    snapshot: dict | None,
    original_runner: TunnelRunner,
    connection_lease: object | None = None,
) -> bool:
    """Restart with a narrow adoption commit after runner.start completes."""
    return await _attempt_tunnel_restart_impl(
        udid,
        ip,
        port,
        snapshot,
        original_runner,
        connection_lease,
        adoption_lock=_get_tunnel_lifecycle_lock(),
    )


async def _per_tunnel_watchdog(
    udid: str,
    runner: TunnelRunner,
    generation: int | None = None,
    *,
    connection_lease: object | None = None,
    connection_engine: object | None = None,
) -> None:
    """Watch a single device's tunnel. If the runner's task dies (WiFi
    blip, iPhone locked, admin revoked), capture the sim state, then try
    up to len(_TUNNEL_RESTART_BACKOFF) restarts with backoff. Each restart
    rebuilds the device manager connection (the new TUN interface gets a
    fresh RSD address) and resumes the sim from snapshot so the iPhone
    keeps moving across the blip. Other tunnels stay isolated."""
    try:
        if connection_lease is not None and connection_engine is None:
            try:
                from main import app_state as _watchdog_state
                connection_engine = _watchdog_state.simulation_engines.get(udid)
            except Exception:
                connection_engine = None

        async def _owns_runner() -> bool:
            async with _tunnels_lock:
                return _tunnel_owner_locked(udid, runner, generation)

        task = runner.task
        if task is None:
            return
        try:
            # Stopping/re-keying a watchdog cancels this observer task.  A
            # direct await would propagate that cancellation into the
            # long-lived runner task and kill the replacement tunnel too.
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # A cancellation raised by the runner itself means its tunnel
            # exited and should enter the normal recovery path.  A caller
            # cancellation belongs to this watchdog and must propagate.
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
        except BaseException:
            pass

        # If the registry was already updated (explicit stop, re-key on
        # reconnect, etc.) this watchdog is stale.
        if not await _owns_runner():
            return

        ip = runner.target_ip
        port = runner.target_port

        _tunnel_logger.warning(
            "Tunnel for %s exited unexpectedly (target=%s:%s); will attempt %d restart(s)",
            udid, ip, port, len(_TUNNEL_RESTART_BACKOFF),
        )
        if not await _owns_runner():
            return
        try:
            from api.websocket import broadcast
            await broadcast("tunnel_degraded", {"udid": udid, "reason": "task_exited"})
        except Exception:
            _tunnel_logger.exception("Failed to emit tunnel_degraded event")

        if ip is None or port is None:
            # No target captured; we have nothing to retry against. Fall
            # through to teardown.
            _tunnel_logger.warning(
                "Tunnel for %s has no captured target ip/port; skipping retries",
                udid,
            )
        else:
            from main import app_state
            snapshot: dict | None = None
            old_eng = app_state.simulation_engines.get(udid)
            if old_eng is not None:
                try:
                    snapshot = old_eng.capture_resumable_snapshot()
                    if snapshot:
                        _tunnel_logger.info(
                            "Captured resumable snapshot for %s before tunnel restart (kind=%s)",
                            udid, snapshot.get("kind"),
                        )
                except Exception:
                    _tunnel_logger.exception("capture_resumable_snapshot failed for %s", udid)

                # Park the engine while we restart. Without this, multi-stop /
                # loop / random-walk keep iterating to the next leg, each call
                # burning ~3s in DvtLocationService._reconnect retries against
                # the dead RSD before raising DeviceLostError, then the handler
                # immediately tries the next leg. The log fills with "Giving up
                # on this route after repeated push failures" every ~6s for as
                # long as the watchdog is mid-restart. Cancelling the active
                # task here halts the thrash; on a successful restart, the
                # snapshot we just captured drives resume_from_snapshot back to
                # the same leg / segment.
                try:
                    from models.schemas import SimulationState as _SS
                    old_eng.state = _SS.DISCONNECTED
                    try:
                        await old_eng._emit("state_change", {"state": old_eng.state.value})
                    except Exception:
                        _tunnel_logger.debug(
                            "Disconnected state_change emit failed during watchdog pause",
                            exc_info=True,
                        )
                    old_eng._stop_event.set()
                    old_eng._pause_event.set()  # unstick anyone awaiting pause_event
                    active = getattr(old_eng, "_active_task", None)
                    if active is not None and not active.done():
                        active.cancel()
                except Exception:
                    _tunnel_logger.exception(
                        "Failed to park engine for %s before tunnel restart", udid,
                    )

            for attempt, delay in enumerate(_TUNNEL_RESTART_BACKOFF, start=1):
                try:
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    return

                # User may have explicitly stopped or replaced this tunnel
                # during the sleep; if so, abort the retry loop.
                if not await _owns_runner():
                    _tunnel_logger.info(
                        "Tunnel for %s no longer registered (user stop?); aborting retries",
                        udid,
                    )
                    return

                _tunnel_logger.info(
                    "Tunnel restart attempt %d/%d for %s (after %.0fs backoff)",
                    attempt, len(_TUNNEL_RESTART_BACKOFF), udid, delay,
                )
                ok = await _attempt_tunnel_restart(
                    udid,
                    ip,
                    port,
                    snapshot,
                    runner,
                    connection_lease=connection_lease,
                )
                if ok:
                    # On success the new watchdog has been armed and this
                    # one's job is done.
                    return
                if not await _owns_runner():
                    return

            # Direct retries against the old endpoint are exhausted. The
            # iPhone likely rebound its RemotePairing port (reboot / WiFi
            # rejoin) or got a new DHCP lease — re-discover before giving
            # up. Each phase runs once; total added time is bounded by
            # one port scan + one discover pass.
            candidates: list[tuple[str, int]] = []
            try:
                candidates = await find_fallback_endpoints(ip)
            except Exception:
                _tunnel_logger.exception(
                    "Fallback endpoint discovery failed for %s", udid,
                )
            if len(candidates) > 8:
                # Bound worst-case time: each candidate attempt can take
                # several seconds, so an unbounded list turns one bad blip
                # into a long thrash. 8 was picked as a generous cap for a
                # home/office LAN scan, not a measured limit.
                _tunnel_logger.info(
                    "Fallback discovery returned %d candidates for %s; trying first 8",
                    len(candidates), udid,
                )
                candidates = candidates[:8]
            for cand_ip, cand_port in candidates:
                if not await _owns_runner():
                    _tunnel_logger.info(
                        "Tunnel for %s no longer registered during fallback; aborting",
                        udid,
                    )
                    return
                if cand_ip == ip and cand_port == port:
                    continue  # the direct-retry loop already tried this exact endpoint
                if any(
                    r is not runner and r.target_ip == cand_ip and r.target_port == cand_port
                    for r in _tunnels.values()
                ):
                    # This endpoint is already claimed by another live
                    # tunnel (e.g. a second, currently-connected iPhone on
                    # the same LAN). Starting a new tunnel against it would
                    # tear down that device's active connection via
                    # connect_wifi_tunnel's implicit disconnect(udid).
                    _tunnel_logger.info(
                        "Skipping fallback candidate %s:%d for %s; claimed by another tunnel",
                        cand_ip, cand_port, udid,
                    )
                    continue
                _tunnel_logger.info(
                    "Fallback restart attempt for %s via %s:%d",
                    udid, cand_ip, cand_port,
                )
                ok = await _attempt_tunnel_restart(
                    udid,
                    cand_ip,
                    cand_port,
                    snapshot,
                    runner,
                    connection_lease=connection_lease,
                )
                if ok:
                    return

        # All retries exhausted (or no target to retry against).
        _tunnel_logger.warning(
            "Tunnel for %s could not be restarted; tearing down WiFi connection",
            udid,
        )
        should_cleanup = False
        wd = None
        side_effects: tuple[asyncio.Task, ...] = ()
        detached_generation: int | None = None
        async with _tunnels_lock:
            current = _tunnels.get(udid)
            if current is runner:
                _tunnels.pop(udid, None)
                wd = _tunnel_watchdogs.pop(udid, None)
                current_generation = _tunnel_generations.get(udid)
                side_effects = _take_tunnel_side_effects_locked(
                    udid,
                    generation=current_generation,
                )
                detached_generation = _next_tunnel_generation_locked(udid)
                should_cleanup = True
        if should_cleanup:
            # This function is normally the watchdog task itself, so do not
            # cancel/await that task while tearing down its registry entry.
            await _stop_tunnel_parts(
                None,
                wd if wd is not asyncio.current_task() else None,
                side_effects=side_effects,
                caller="watchdog_tunnel_died",
                udid=udid,
            )
            cleaned = False
            if connection_lease is not None or connection_engine is not None:
                cleaned = await _cleanup_wifi_connection_for(
                    udid,
                    expected_connection=connection_lease,
                    expected_engine=connection_engine,
                    caller="watchdog_tunnel_died",
                )
            else:
                cleaned = True
            # The old watchdog may have detached while a replacement won the
            # same-UDID slot during engine/lease cleanup.  Serialize the final
            # decision with every WiFi/standalone adoption commit.  The lock
            # order is lifecycle -> DeviceManager -> registry; websocket I/O
            # is intentionally after releasing _tunnels_lock, but remains
            # under the production DM lock so C2 cannot overtake the check.
            lifecycle_lock = _get_tunnel_lifecycle_lock()
            async with lifecycle_lock:
                try:
                    dm = _dm()
                except Exception:
                    dm = None
                try:
                    from main import app_state as _watchdog_state
                except Exception:
                    _watchdog_state = None

                async def _finish_tunnel_lost() -> None:
                    # Registry/generation ownership is a brief snapshot.  Do
                    # not hold _tunnels_lock across the awaited websocket
                    # broadcast (or any other I/O).
                    async with _tunnels_lock:
                        replacement_present = (
                            _tunnels.get(udid) is not None
                            or (
                                detached_generation is not None
                                and _tunnel_generations.get(udid)
                                != detached_generation
                            )
                        )

                    try:
                        current_connection = (
                            dm._connections.get(udid) if dm is not None else None
                        )
                        current_engine = (
                            _watchdog_state.simulation_engines.get(udid)
                            if _watchdog_state is not None
                            else None
                        )
                    except Exception:
                        current_connection = None
                        current_engine = None
                    replacement_present = replacement_present or (
                        current_connection is not None
                        and current_connection is not connection_lease
                    ) or (
                        current_engine is not None
                        and current_engine is not connection_engine
                    )
                    cleanup_complete = cleaned or (
                        current_connection is None and current_engine is None
                    )
                    if cleanup_complete and not replacement_present:
                        try:
                            from api.websocket import broadcast
                            await broadcast(
                                "tunnel_lost",
                                {"udid": udid, "reason": "task_exited"},
                            )
                        except Exception:
                            _tunnel_logger.exception(
                                "Failed to emit tunnel_lost event"
                            )

                dm_lock = getattr(dm, "_lock", None) if dm is not None else None
                if dm_lock is None:
                    # Lightweight test doubles may omit DeviceManager's lock;
                    # lifecycle + registry fencing still protects their path.
                    await _finish_tunnel_lost()
                else:
                    async with dm_lock:
                        await _finish_tunnel_lost()
    except asyncio.CancelledError:
        raise
    except Exception:
        # Anything unhandled here (e.g. an exception leaking out of
        # _attempt_tunnel_restart) would otherwise die silently as an
        # unobserved task exception — log it so a crashed watchdog is at
        # least visible instead of just going quiet.
        _tunnel_logger.exception("Watchdog for %s crashed unexpectedly", udid)

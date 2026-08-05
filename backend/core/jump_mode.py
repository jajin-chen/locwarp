"""Shared jump-mode core -- teleport point-to-point through waypoints.

Both the route looper (closed loop, lap limits) and the multi-stop
navigator (open sequence, optional loop) delegate their jump-mode
simulation to :func:`run_jump_sequence`. The speed-profile picker and
per-station pause sampler shared by the walking modes live here too so
route_loop / multi_stop / flower don't each carry a private copy.
"""

from __future__ import annotations

import asyncio
import logging
import random

from models.schemas import Coordinate, SimulationState
from config import resolve_speed_profile

logger = logging.getLogger(__name__)


def pick_speed_profile(
    engine,
    profile_name: str,
    speed_kmh: float | None,
    speed_min_kmh: float | None,
    speed_max_kmh: float | None,
) -> dict:
    """Resolve the speed profile for the next leg / lap. A speed applied
    mid-flight (apply_speed) takes precedence; otherwise re-resolve from
    the original args (so range mode produces per-leg / per-lap
    variation)."""
    if engine._speed_was_applied and engine._active_speed_profile is not None:
        return dict(engine._active_speed_profile)
    return resolve_speed_profile(
        profile_name, speed_kmh, speed_min_kmh, speed_max_kmh,
    )


def sample_pause_seconds(pause_enabled: bool, pause_min: float, pause_max: float) -> float:
    """Per-station pause sampler. Returns a non-negative duration; 0 means
    "skip the pause entirely"."""
    if not pause_enabled:
        return 0.0
    lo, hi = sorted((float(pause_min), float(pause_max)))
    if lo < 0:
        lo = 0.0
    if hi <= 0:
        return 0.0
    return random.uniform(lo, hi)


async def jump_wait(engine, seconds: float, *, source: str) -> bool:
    """Sleep for *seconds*, honouring both stop and pause.

    - Stop wakes the wait immediately and returns True.
    - Pause freezes the remaining countdown: when resumed, the leftover
      time runs to completion. Without this, pause was a no-op in jump
      mode (issue #32) — the next teleport fired regardless.

    Polls in 100 ms slices so a pause that lands mid-delay takes effect
    promptly without spinning a dedicated watcher task.
    """
    remaining = max(0.0, float(seconds))
    emitted = False
    # Keep the WiFi tunnel fed during the dwell. The engine pushes nothing
    # while it just sleeps here, so on a screen-off iPhone the socket can go
    # quiet long enough for iOS to reap it. Re-push the current (frozen)
    # coordinate every ~1s, mirroring the idle keepalive — it both keeps the
    # fake location pinned and gives the tunnel traffic to stay alive.
    since_push = 0.0
    KEEPALIVE_EVERY = 1.0
    try:
        while True:
            if engine._stop_event.is_set():
                return True
            if not engine._pause_event.is_set():
                pause_task = asyncio.ensure_future(engine._pause_event.wait())
                stop_task = asyncio.ensure_future(engine._stop_event.wait())
                try:
                    await asyncio.wait(
                        {pause_task, stop_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    for t in (pause_task, stop_task):
                        if not t.done():
                            t.cancel()
                if engine._stop_event.is_set():
                    return True
            if remaining <= 0:
                return False
            if not emitted and seconds > 0:
                await engine._emit("pause_countdown", {
                    "duration_seconds": seconds,
                    "source": source,
                })
                emitted = True
            slice_s = min(remaining, 0.1)
            try:
                await asyncio.wait_for(engine._stop_event.wait(), timeout=slice_s)
                return True
            except asyncio.TimeoutError:
                remaining -= slice_s
                since_push += slice_s
                if since_push >= KEEPALIVE_EVERY:
                    since_push = 0.0
                    pos = engine.current_position
                    if pos is not None:
                        try:
                            await engine.location_service.set(pos.lat, pos.lng)
                        except Exception:
                            logger.debug(
                                "jump_wait keepalive re-push failed", exc_info=True,
                            )
    finally:
        if emitted:
            await engine._emit("pause_countdown_end", {"source": source})


async def run_jump_sequence(
    engine,
    waypoints: list[Coordinate],
    *,
    pre_delay: float,
    post_delay: float,
    state: SimulationState,
    source: str,
    close_loop: bool,
    repeat: bool,
    lap_limit: int | None,
    emit_stop_reached: bool,
    state_change_extra: dict | None = None,
    complete_event: str | None = None,
) -> None:
    """Teleport sequentially through *waypoints*. Each stop is preceded by
    *pre_delay* seconds and followed by *post_delay* seconds. Stops
    cleanly when ``engine._stop_event`` is set; pause freezes both delays.

    Parameters select the mode flavour:

    - ``close_loop`` (route loop): teleport back to waypoints[0] before
      counting each lap so the visible path closes; laps are counted and
      announced with their total, and ``lap_limit`` auto-stops the run
      with a ``loop_complete`` event.
    - ``repeat``: start another pass after the last stop. When False, the
      post-delay after the final stop is skipped — the simulation is
      finished, so the wait would just delay the IDLE transition.
    - ``emit_stop_reached`` (multi-stop): emit ``stop_reached`` at each
      stop.
    - ``state_change_extra``: extra fields merged into the initial
      ``state_change`` payload.
    - ``complete_event``: event emitted with ``{"laps": ...}`` on
      teardown (e.g. ``multi_stop_complete``).
    """
    engine.state = state
    engine.total_segments = len(waypoints)
    engine.lap_count = 0
    engine.segment_index = 0
    engine.distance_traveled = 0.0
    engine.distance_remaining = 0.0
    engine._user_waypoints = list(waypoints)
    engine._user_waypoint_next = 1 if len(waypoints) > 1 else 0

    await engine._emit("route_path", {
        "coords": [{"lat": wp.lat, "lng": wp.lng} for wp in waypoints],
    })
    await engine._emit("state_change", {
        "state": engine.state.value,
        "waypoints": [{"lat": wp.lat, "lng": wp.lng} for wp in waypoints],
        **(state_change_extra or {}),
    })

    if close_loop:
        logger.info(
            "Jump loop started: %d waypoints, pre=%.1fs post=%.1fs, laps=%s",
            len(waypoints), pre_delay, post_delay, lap_limit or "∞",
        )
    else:
        logger.info(
            "Jump multi-stop started: %d waypoints, pre=%.1fs post=%.1fs, loop=%s",
            len(waypoints), pre_delay, post_delay, repeat,
        )

    limit = lap_limit if (lap_limit is not None and lap_limit > 0) else None

    while not engine._stop_event.is_set():
        for i, wp in enumerate(waypoints):
            if engine._stop_event.is_set():
                break
            if await jump_wait(engine, pre_delay, source=source):
                break
            if engine._stop_event.is_set():
                break
            await engine._set_position(wp.lat, wp.lng)
            engine.segment_index = i
            engine._user_waypoint_next = min(i + 1, len(waypoints))
            await engine._emit("position_update", {
                "lat": wp.lat, "lng": wp.lng,
                "speed_mps": 0.0,
                "progress": (i + 1) / max(len(waypoints), 1),
                "segment_index": i,
                "total_segments": len(waypoints),
                "lap_count": engine.lap_count,
                "distance_traveled": 0.0,
                "distance_remaining": 0.0,
                "eta_seconds": 0.0,
                "eta_arrival": "",
                "is_paused": False,
            })
            await engine._emit("user_waypoint_advance", {
                "current_index": i,
                "next_index": min(i + 1, len(waypoints) - 1),
            })
            if emit_stop_reached:
                await engine._emit("stop_reached", {
                    "index": i + 1,
                    "total": len(waypoints),
                    "lat": wp.lat, "lng": wp.lng,
                })
            # Skip the post-delay after the very last stop of a non-repeating
            # run — the simulation is finished, so the wait would just
            # delay the IDLE transition without serving any purpose.
            if not repeat and i == len(waypoints) - 1:
                continue
            if await jump_wait(engine, post_delay, source=source):
                break

        if engine._stop_event.is_set():
            break

        # Teleport back to start before counting the lap so the visible
        # path closes (only relevant for the closed-loop mode).
        if close_loop:
            wp0 = waypoints[0]
            await engine._set_position(wp0.lat, wp0.lng)
            await engine._emit("position_update", {
                "lat": wp0.lat, "lng": wp0.lng,
                "speed_mps": 0.0, "progress": 1.0,
                "segment_index": 0, "total_segments": len(waypoints),
                "lap_count": engine.lap_count + 1,
                "distance_traveled": 0.0, "distance_remaining": 0.0,
                "eta_seconds": 0.0, "eta_arrival": "", "is_paused": False,
            })

        if not repeat:
            break

        engine.lap_count += 1
        if close_loop:
            await engine._emit("lap_complete", {
                "lap": engine.lap_count, "total": limit,
            })
            logger.info("Jump loop lap %d%s complete",
                        engine.lap_count, f"/{limit}" if limit else "")
        else:
            await engine._emit("lap_complete", {"lap": engine.lap_count})
            logger.info("Jump multi-stop lap %d complete", engine.lap_count)

        if limit is not None and engine.lap_count >= limit:
            await engine._emit("loop_complete", {"laps": engine.lap_count})
            break

    if engine.state == state:
        engine.state = SimulationState.IDLE
        if complete_event:
            await engine._emit(complete_event, {"laps": engine.lap_count})
        await engine._emit("state_change", {"state": engine.state.value})

    if close_loop:
        logger.info("Jump loop stopped after %d laps", engine.lap_count)
    else:
        logger.info("Jump multi-stop finished after %d laps", engine.lap_count)

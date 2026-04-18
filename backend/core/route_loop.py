"""Route looper -- infinitely loop through a closed route."""

from __future__ import annotations

import asyncio
import logging
import random

from models.schemas import Coordinate, MovementMode, SimulationState
from config import resolve_speed_profile

logger = logging.getLogger(__name__)


class RouteLooper:
    """Creates a closed route through waypoints and loops it indefinitely."""

    def __init__(self, engine):
        self.engine = engine

    async def start_loop(
        self,
        waypoints: list[Coordinate],
        mode: MovementMode,
        *,
        speed_kmh: float | None = None,
        speed_min_kmh: float | None = None,
        speed_max_kmh: float | None = None,
        pause_enabled: bool = True,
        pause_min: float = 5.0,
        pause_max: float = 20.0,
        straight_line: bool = False,
        lap_count: int | None = None,
    ) -> None:
        """Build a multi-waypoint route that forms a closed loop, then
        traverse it repeatedly until stopped.

        Parameters
        ----------
        waypoints
            Ordered waypoints forming the loop. The route will be closed
            by appending the first waypoint at the end.
        mode
            Movement mode determining speed profile.
        """
        engine = self.engine

        if len(waypoints) < 2:
            raise ValueError("At least 2 waypoints are required for a loop")

        profile_name = mode.value
        osrm_profile = "foot" if mode in (MovementMode.WALKING, MovementMode.RUNNING) else "car"

        # Close the loop: append the first waypoint at the end
        closed_waypoints = list(waypoints) + [waypoints[0]]

        # Build OSRM route through all waypoints
        wp_tuples = [(wp.lat, wp.lng) for wp in closed_waypoints]
        route_data = await engine.route_service.get_multi_route(
            wp_tuples, profile=osrm_profile,
            force_straight=straight_line,
        )

        coords = [Coordinate(lat=pt[0], lng=pt[1]) for pt in route_data["coords"]]

        if len(coords) < 2:
            raise ValueError("OSRM returned an empty route for the loop")

        # Resume support: when we're taking over a loop from a peer
        # engine that just disconnected, jump straight to the segment
        # they were on and inherit their lap count instead of starting
        # the closed-loop traversal at coords[0] (which would teleport
        # the iPhone back to waypoints[0]).
        resume_snap = engine._resume_snapshot if engine._resume_snapshot and engine._resume_snapshot.get("kind") == "start_loop" else None
        engine._resume_snapshot = None

        engine.state = SimulationState.LOOPING
        engine.total_segments = len(coords) - 1
        if resume_snap:
            engine.lap_count = int(resume_snap.get("lap_count", 0))
            resume_seg = max(0, min(int(resume_snap.get("segment_index", 0)), len(coords) - 1))
            resume_uwn = int(resume_snap.get("user_waypoint_next", 1))
        else:
            engine.lap_count = 0
            resume_seg = 0
            resume_uwn = 1 if len(waypoints) > 1 else 0
        engine.segment_index = resume_seg

        await engine._emit("route_path", {
            "coords": [{"lat": c.lat, "lng": c.lng} for c in coords],
        })
        await engine._emit("state_change", {
            "state": engine.state.value,
            "waypoints": [{"lat": wp.lat, "lng": wp.lng} for wp in waypoints],
        })

        logger.info("Starting route loop with %d waypoints [%s]%s",
                    len(waypoints), profile_name,
                    f" (resuming at segment {resume_seg}, lap {engine.lap_count})" if resume_snap else "")

        first_iteration = True
        # Loop until stopped
        while not engine._stop_event.is_set():
            engine.distance_traveled = 0.0
            engine.distance_remaining = route_data["distance"]
            engine.segment_index = resume_seg if first_iteration else 0

            # Tell _move_along_route which user-facing waypoints to track for
            # waypoint_progress emission (we close the loop on the road but
            # the UI only shows the named waypoints the user entered).
            engine._user_waypoints = list(waypoints)
            # Restart highlight from waypoint[1] each lap so UI re-pulses,
            # except on the first lap of a resume where we inherit the
            # previous engine's progress.
            engine._user_waypoint_next = (
                resume_uwn if first_iteration else (1 if len(waypoints) > 1 else 0)
            )

            # If the user has applied a speed mid-flight, honor it on
            # subsequent laps; otherwise re-pick speed each lap so a range
            # produces realistic per-lap variation.
            if engine._speed_was_applied and engine._active_speed_profile is not None:
                speed_profile = dict(engine._active_speed_profile)
            else:
                speed_profile = resolve_speed_profile(
                    profile_name, speed_kmh, speed_min_kmh, speed_max_kmh,
                )

            # On the first iteration of a resume, splice the iPhone's
            # actual current position onto the front of the remaining
            # coords so _move_along_route's first emit lands on the
            # device's existing GPS reading instead of teleporting it
            # back to coords[resume_seg].
            if first_iteration and resume_seg > 0:
                cur = engine.current_position
                tail = coords[resume_seg:]
                if cur is not None:
                    traversal = [Coordinate(lat=cur.lat, lng=cur.lng)] + tail
                else:
                    traversal = tail
                await engine._move_along_route(traversal, speed_profile)
            else:
                await engine._move_along_route(coords, speed_profile)
            first_iteration = False

            # Check if we were stopped during the route
            if engine._stop_event.is_set():
                break

            engine.lap_count += 1
            # lap_count <= 0 is treated as "unlimited" (same as None) so the
            # field is safe to accept from schema even if client sends 0.
            limit = lap_count if (lap_count is not None and lap_count > 0) else None
            await engine._emit("lap_complete", {
                "lap": engine.lap_count,
                "total": limit,
            })
            logger.info(
                "Loop lap %d%s complete",
                engine.lap_count,
                f"/{limit}" if limit else "",
            )

            # Auto-stop after the requested number of laps.
            if limit is not None and engine.lap_count >= limit:
                logger.info("Loop reached configured lap count %d, stopping", limit)
                break

            # Optional random pause between laps
            if pause_enabled:
                lo, hi = sorted((float(pause_min), float(pause_max)))
                if lo < 0:
                    lo = 0.0
                if hi > 0:
                    lap_pause = random.uniform(lo, hi)
                    logger.info("Loop: pausing %.1fs before next lap", lap_pause)
                    await engine._emit("pause_countdown", {
                        "duration_seconds": lap_pause,
                        "source": "loop",
                    })
                    try:
                        await asyncio.wait_for(engine._stop_event.wait(), timeout=lap_pause)
                        break
                    except asyncio.TimeoutError:
                        pass
                    await engine._emit("pause_countdown_end", {"source": "loop"})

        if engine.state == SimulationState.LOOPING:
            engine.state = SimulationState.IDLE
            await engine._emit("state_change", {"state": engine.state.value})

        logger.info("Route loop stopped after %d laps", engine.lap_count)

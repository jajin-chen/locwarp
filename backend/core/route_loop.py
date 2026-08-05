"""Route looper -- infinitely loop through a closed route."""

from __future__ import annotations

import asyncio
import logging

from models.schemas import Coordinate, MovementMode, SimulationState
from core.jump_mode import pick_speed_profile, run_jump_sequence, sample_pause_seconds

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
        route_engine: str | None = None,
        lap_count: int | None = None,
        jump_mode: bool = False,
        jump_pre_delay: float = 2.0,
        jump_post_delay: float = 4.0,
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

        # Jump mode: teleport point-to-point with configurable pre / post
        # delays instead of walking. Skips OSRM routing entirely. Resume /
        # per-station random pause / speed profile are not used in this
        # mode because there's no continuous movement to interpolate.
        if jump_mode:
            await run_jump_sequence(
                engine,
                waypoints,
                pre_delay=max(0.0, float(jump_pre_delay)),
                post_delay=max(0.0, float(jump_post_delay)),
                state=SimulationState.LOOPING,
                source="loop",
                close_loop=True,
                repeat=True,
                lap_limit=lap_count,
                emit_stop_reached=False,
            )
            return

        profile_name = mode.value
        osrm_profile = "foot" if mode in (MovementMode.WALKING, MovementMode.RUNNING) else "car"

        # Close the loop: append the first waypoint at the end
        closed_waypoints = list(waypoints) + [waypoints[0]]

        # Build OSRM route through all waypoints
        wp_tuples = [(wp.lat, wp.lng) for wp in closed_waypoints]
        route_data = await engine.route_service.get_multi_route(
            wp_tuples, profile=osrm_profile,
            force_straight=straight_line,
            engine=route_engine,
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

        async def _pause_at_stop(stop_index: int) -> bool:
            """Pause for a random duration. Returns True if the simulation was
            stopped during the pause (caller should break out of its loop)."""
            secs = sample_pause_seconds(pause_enabled, pause_min, pause_max)
            if secs <= 0:
                return False
            logger.info("Loop: pausing %.1fs at stop %d", secs, stop_index)
            await engine._emit("pause_countdown", {
                "duration_seconds": secs,
                "source": "loop",
            })
            try:
                await asyncio.wait_for(engine._stop_event.wait(), timeout=secs)
                return True
            except asyncio.TimeoutError:
                pass
            await engine._emit("pause_countdown_end", {"source": "loop"})
            return False

        # Grand-total lap distance, used to drive a whole-lap ETA in the EtaBar
        # (the per-leg distance set by _move_along_route otherwise resets the
        # countdown at every waypoint).
        full_total_distance = float(route_data.get("distance") or 0.0)

        first_iteration = True
        # Loop until stopped. Each iteration walks one full lap by routing
        # leg-by-leg between user waypoints (mirrors multi_stop) so we can
        # pause at each station, not just between laps.
        while not engine._stop_event.is_set():
            engine.distance_traveled = 0.0
            engine.distance_remaining = route_data["distance"]
            engine.segment_index = 0

            engine._user_waypoints = list(waypoints)
            engine._user_waypoint_next = (
                resume_uwn if first_iteration else (1 if len(waypoints) > 1 else 0)
            )

            # Re-picked per lap: a speed applied mid-flight wins; otherwise
            # range mode produces per-lap variation.
            speed_profile = pick_speed_profile(
                engine, profile_name, speed_kmh, speed_min_kmh, speed_max_kmh,
            )

            # Tracks meters already walked this lap so we can compute the
            # leftover whole-lap distance handed to _route_offset_remaining
            # before each leg.
            completed_distance = 0.0

            # Walk station-by-station around the closed loop.
            # closed_waypoints already has waypoints[0] appended at the end so
            # the iteration covers all legs back to the start. On a resume,
            # start at the leg the previous engine was on (resume_seg now
            # represents a leg index rather than the old densified-coord
            # index since we walk leg-by-leg).
            num_legs = len(closed_waypoints) - 1
            leg_start = resume_seg if (first_iteration and resume_snap) else 0
            leg_start = max(0, min(leg_start, num_legs - 1))
            for leg_idx in range(leg_start, num_legs):
                if engine._stop_event.is_set():
                    break

                wp_a = closed_waypoints[leg_idx]
                wp_b = closed_waypoints[leg_idx + 1]
                engine.segment_index = leg_idx

                # Resume support: on the first leg of the first lap after
                # taking over (peer handoff or live waypoint splice), start
                # from the iPhone's actual GPS instead of routing back to
                # wp_a (which would teleport to the previous waypoint).
                # Compares against leg_start, not 0, so a splice that
                # resumes at leg_idx > 0 still uses current_position.
                if first_iteration and leg_idx == leg_start and resume_snap and engine.current_position is not None:
                    leg_origin = engine.current_position
                else:
                    leg_origin = wp_a

                # Per-leg OSRM route (cheap because legs are small).
                leg_route = await engine.route_service.get_route(
                    leg_origin.lat, leg_origin.lng,
                    wp_b.lat, wp_b.lng,
                    profile=osrm_profile,
                    force_straight=straight_line,
                    engine=route_engine,
                )
                leg_coords = [Coordinate(lat=pt[0], lng=pt[1]) for pt in leg_route["coords"]]
                leg_distance = float(leg_route.get("distance") or 0.0)
                # Whole-lap ETA: feed _move_along_route the distance left in
                # *future* legs so the EtaBar reflects "time to close the lap"
                # rather than "time to next waypoint".
                if full_total_distance > 0:
                    future_legs = max(
                        full_total_distance - completed_distance - leg_distance,
                        0.0,
                    )
                else:
                    future_legs = 0.0
                engine._route_offset_remaining = future_legs

                if len(leg_coords) >= 2:
                    await engine._move_along_route(leg_coords, speed_profile)

                completed_distance += leg_distance
                engine._route_offset_remaining = 0.0

                if engine._stop_event.is_set():
                    break

                # Pause at every stop except the last one of the lap (the
                # closing leg lands back on waypoints[0], which becomes the
                # start of the next lap — no double-pause needed).
                is_last_leg = leg_idx == num_legs - 1
                if not is_last_leg:
                    if await _pause_at_stop(leg_idx + 1):
                        break

            first_iteration = False

            if engine._stop_event.is_set():
                break

            engine.lap_count += 1
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
                # Surface a dedicated completion event so the frontend can
                # play its route-completion alert sound. We deliberately
                # only emit this in the auto-stop path, not when the user
                # hits Stop or an infinite loop is interrupted.
                await engine._emit("loop_complete", {"laps": engine.lap_count})
                break

            # No between-laps pause — the per-station pause already covers
            # the rest stops; jumping straight into the next lap keeps the
            # behaviour symmetric with what the user expects.

        engine._route_offset_remaining = 0.0
        if engine.state == SimulationState.LOOPING:
            engine.state = SimulationState.IDLE
            await engine._emit("state_change", {"state": engine.state.value})

        logger.info("Route loop stopped after %d laps", engine.lap_count)

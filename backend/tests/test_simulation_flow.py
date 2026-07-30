"""Simulation engine flow test — the full lifecycle the watchdog exercises
in production, against a fake location service (no iPhone, no OSRM):

    teleport → start_loop → disconnect mid-lap → snapshot → resume on a
    fresh engine → stop

straight_line=True keeps routing fully offline; a very high speed keeps
the whole cycle under a couple of seconds of wall-clock.
"""

from __future__ import annotations

import asyncio

import pytest

from core.simulation_engine import SimulationEngine
from models.schemas import Coordinate, MovementMode, SimulationState
from fakes import FakeLocationService

# ~55 m legs in downtown Taipei; at 2000 km/h each leg is a couple ticks.
WAYPOINTS = [
    Coordinate(lat=25.0330, lng=121.5654),
    Coordinate(lat=25.0335, lng=121.5654),
    Coordinate(lat=25.0335, lng=121.5659),
]


async def wait_until(predicate, timeout=5.0, interval=0.02) -> None:
    """Poll *predicate* until true or fail the test after *timeout*."""
    deadline = asyncio.get_event_loop().time() + timeout
    while not predicate():
        if asyncio.get_event_loop().time() > deadline:
            pytest.fail("condition not reached within timeout")
        await asyncio.sleep(interval)


def make_engine() -> tuple[SimulationEngine, FakeLocationService, list]:
    svc = FakeLocationService()
    events: list[tuple[str, dict]] = []

    async def callback(event_type: str, data: dict) -> None:
        events.append((event_type, data))

    return SimulationEngine(svc, callback), svc, events


async def test_teleport_sets_position_and_returns_to_idle():
    engine, svc, events = make_engine()

    pos = await engine.teleport(25.0330, 121.5654)

    assert (pos.lat, pos.lng) == (25.0330, 121.5654)
    assert svc.positions == [(25.0330, 121.5654)]
    assert engine.state == SimulationState.IDLE
    assert engine.current_position is not None
    event_types = [t for t, _ in events]
    assert "teleport" in event_types
    assert "position_update" in event_types


async def test_full_cycle_loop_disconnect_snapshot_resume():
    # ── Phase 1: teleport + loop on engine 1 ──
    eng1, svc1, events1 = make_engine()
    await eng1.teleport(25.0330, 121.5654)

    loop_task = asyncio.create_task(eng1.start_loop(
        WAYPOINTS, MovementMode.WALKING,
        speed_kmh=2000, pause_enabled=False, straight_line=True,
    ))
    await wait_until(
        lambda: eng1.state == SimulationState.LOOPING and len(svc1.positions) >= 4,
    )
    assert any(t == "route_path" for t, _ in events1)

    # ── Phase 2: disconnect mid-lap (mirror of the watchdog teardown in
    # main.py / api/device.py: mark DISCONNECTED, stop, cancel) ──
    snapshot = eng1.capture_resumable_snapshot()
    assert snapshot is not None
    assert snapshot["kind"] == "start_loop"
    assert snapshot["current_pos"] is not None

    eng1.state = SimulationState.DISCONNECTED
    eng1._stop_event.set()
    eng1._pause_event.set()
    if eng1._active_task is not None and not eng1._active_task.done():
        eng1._active_task.cancel()
    await loop_task
    assert eng1.state == SimulationState.DISCONNECTED

    # ── Phase 3: resume the same sim on a fresh engine (new device) ──
    eng2, svc2, _events2 = make_engine()
    resume_task = asyncio.create_task(eng2.resume_from_snapshot(snapshot))
    await wait_until(
        lambda: eng2.state == SimulationState.LOOPING and len(svc2.positions) >= 2,
    )

    # First push on the new device is the exact captured position — the
    # handoff teleport that prevents a visible jump.
    assert svc2.positions[0] == tuple(snapshot["current_pos"])
    # Lap progress carried over instead of restarting from zero.
    assert eng2.lap_count >= snapshot["lap_count"]

    # ── Phase 4: clean stop ──
    await eng2.stop()
    await resume_task
    assert eng2.state == SimulationState.IDLE


async def test_stop_keeps_last_position():
    """/stop halts movement but must NOT clear the simulated location."""
    engine, svc, _events = make_engine()
    await engine.teleport(25.0330, 121.5654)

    loop_task = asyncio.create_task(engine.start_loop(
        WAYPOINTS, MovementMode.WALKING,
        speed_kmh=2000, pause_enabled=False, straight_line=True,
    ))
    await wait_until(lambda: engine.state == SimulationState.LOOPING)

    await engine.stop()
    await loop_task

    assert engine.state == SimulationState.IDLE
    assert engine.current_position is not None
    assert svc.clear_count == 0

from fastapi import APIRouter, HTTPException

from models.schemas import (
    TeleportRequest,
    NavigateRequest,
    LoopRequest,
    MultiStopRequest,
    RandomWalkRequest,
    JoystickStartRequest,
    SimulationStatus,
    Coordinate,
    CooldownSettings,
    CooldownStatus,
    CoordFormatRequest,
    CoordinateFormat,
)

router = APIRouter(prefix="/api/location", tags=["location"])


async def _engine():
    """Return the active SimulationEngine, lazily rebuilding it if a device
    is actually connected but the engine slot is empty (e.g. DVT setup failed
    during startup auto-connect, iPad screen was locked, etc.)."""
    from main import app_state
    import logging as _logging
    _log = _logging.getLogger("locwarp")

    if app_state.simulation_engine is not None:
        return app_state.simulation_engine

    # Engine missing — try to rebuild from any currently connected device
    dm = app_state.device_manager
    connected_udids = list(dm._connections.keys())
    if connected_udids:
        udid = connected_udids[0]
        _log.info("simulation_engine missing; attempting lazy rebuild for %s", udid)
        try:
            await app_state.create_engine_for_device(udid)
            if app_state.simulation_engine is not None:
                return app_state.simulation_engine
        except Exception:
            _log.exception("Lazy engine rebuild failed for %s", udid)

    raise HTTPException(
        status_code=400,
        detail={"code": "no_device", "message": "尚未連接任何 iOS 裝置,請先透過 USB 連線"},
    )


def _cooldown():
    from main import app_state
    return app_state.cooldown_timer


def _coord_fmt():
    from main import app_state
    return app_state.coord_formatter


# ── Simulation modes ─────────────────────────────────────

@router.post("/teleport")
async def teleport(req: TeleportRequest):
    engine = await _engine()
    cooldown = _cooldown()

    # Enforce cooldown server-side: if enabled and currently active,
    # refuse the teleport so API clients cannot bypass the UI guard.
    if cooldown.enabled and cooldown.is_active and cooldown.remaining > 0:
        raise HTTPException(
            status_code=429,
            detail={
                "code": "cooldown_active",
                "message": f"冷卻中,還需等待 {int(cooldown.remaining)} 秒",
                "remaining_seconds": cooldown.remaining,
            },
        )

    old_pos = engine.current_position
    try:
        await engine.teleport(req.lat, req.lng)
    except HTTPException:
        raise
    except Exception as e:
        import traceback, logging
        logging.getLogger("locwarp").error("Teleport failed:\n%s", traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))

    # Start cooldown if enabled and there was a previous position
    if old_pos and cooldown.enabled:
        await cooldown.start(old_pos.lat, old_pos.lng, req.lat, req.lng)

    return {"status": "ok", "lat": req.lat, "lng": req.lng}


@router.post("/navigate")
async def navigate(req: NavigateRequest):
    import asyncio
    engine = await _engine()
    asyncio.create_task(engine.navigate(
        Coordinate(lat=req.lat, lng=req.lng), req.mode,
        speed_kmh=req.speed_kmh,
        speed_min_kmh=req.speed_min_kmh, speed_max_kmh=req.speed_max_kmh,
    ))
    return {"status": "started", "destination": {"lat": req.lat, "lng": req.lng}, "mode": req.mode}


@router.post("/loop")
async def loop(req: LoopRequest):
    import asyncio
    engine = await _engine()
    asyncio.create_task(engine.start_loop(
        req.waypoints, req.mode,
        speed_kmh=req.speed_kmh,
        speed_min_kmh=req.speed_min_kmh, speed_max_kmh=req.speed_max_kmh,
    ))
    return {"status": "started", "waypoints": len(req.waypoints), "mode": req.mode}


@router.post("/multistop")
async def multi_stop(req: MultiStopRequest):
    import asyncio
    engine = await _engine()
    asyncio.create_task(engine.multi_stop(
        req.waypoints, req.mode, req.stop_duration, req.loop,
        speed_kmh=req.speed_kmh,
        speed_min_kmh=req.speed_min_kmh, speed_max_kmh=req.speed_max_kmh,
    ))
    return {"status": "started", "stops": len(req.waypoints), "mode": req.mode}


@router.post("/randomwalk")
async def random_walk(req: RandomWalkRequest):
    import asyncio
    engine = await _engine()
    asyncio.create_task(engine.random_walk(
        req.center, req.radius_m, req.mode, req.min_pause, req.max_pause,
        speed_kmh=req.speed_kmh,
        speed_min_kmh=req.speed_min_kmh, speed_max_kmh=req.speed_max_kmh,
    ))
    return {"status": "started", "radius_m": req.radius_m, "mode": req.mode}


@router.post("/joystick/start")
async def joystick_start(req: JoystickStartRequest):
    engine = await _engine()
    try:
        await engine.joystick_start(req.mode)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"status": "started", "mode": req.mode}


@router.post("/joystick/stop")
async def joystick_stop():
    engine = await _engine()
    await engine.joystick_stop()
    return {"status": "stopped"}


@router.post("/pause")
async def pause():
    engine = await _engine()
    await engine.pause()
    return {"status": "paused"}


@router.post("/resume")
async def resume():
    engine = await _engine()
    await engine.resume()
    return {"status": "resumed"}


@router.post("/restore")
async def restore():
    engine = await _engine()
    await engine.restore()
    return {"status": "restored"}


@router.delete("/simulation")
async def stop_simulation():
    engine = await _engine()
    await engine.restore()
    return {"status": "stopped"}


@router.get("/debug")
async def debug_info():
    """Debug endpoint to check engine and location service state."""
    from main import app_state
    engine = app_state.simulation_engine
    if engine is None:
        return {"engine": None}
    loc_svc = engine.location_service
    return {
        "engine": type(engine).__name__,
        "state": engine.state.value if engine.state else None,
        "current_position": {"lat": engine.current_position.lat, "lng": engine.current_position.lng} if engine.current_position else None,
        "location_service": type(loc_svc).__name__ if loc_svc else None,
        "location_service_active": getattr(loc_svc, '_active', None),
    }


@router.get("/status", response_model=SimulationStatus)
async def get_status():
    engine = await _engine()
    status = engine.get_status()
    cooldown = _cooldown()
    cs = cooldown.get_status()
    status.cooldown_remaining = cs["remaining_seconds"]
    return status


# ── Cooldown ──────────────────────────────────────────────

@router.get("/cooldown/status", response_model=CooldownStatus, tags=["cooldown"])
async def cooldown_status():
    cd = _cooldown()
    s = cd.get_status()
    return CooldownStatus(**s)


@router.put("/cooldown/settings", tags=["cooldown"])
async def cooldown_settings(req: CooldownSettings):
    cd = _cooldown()
    cd.enabled = req.enabled
    if not req.enabled:
        await cd.dismiss()
    return {"enabled": cd.enabled}


@router.post("/cooldown/dismiss", tags=["cooldown"])
async def cooldown_dismiss():
    cd = _cooldown()
    await cd.dismiss()
    return {"status": "dismissed"}


# ── Coordinate format ────────────────────────────────────

@router.get("/settings/coord-format", tags=["settings"])
async def get_coord_format():
    fmt = _coord_fmt()
    return {"format": fmt.format.value}


@router.put("/settings/coord-format", tags=["settings"])
async def set_coord_format(req: CoordFormatRequest):
    fmt = _coord_fmt()
    fmt.format = req.format
    return {"format": fmt.format.value}

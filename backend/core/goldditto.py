"""Gold-Ditto (拉金盆) handler.

Two-step cycle: push device GPS to A, then immediately restore real
GPS. The user opens the in-game flower bud manually before pressing
the button, which freezes the game state during the animation window
— the cycle only needs to anchor the simulated location at A then
hand off cleanly to real GPS, no wait, no alternation.
"""
from __future__ import annotations

import logging

from models.schemas import SimulationState

logger = logging.getLogger(__name__)


class GoldDittoHandler:
    def __init__(self, engine):
        self.engine = engine

    async def cycle(self, lat: float, lng: float) -> None:
        # Skip engine.teleport(): it emits a position_update WS event, which
        # makes the frontend auto-center the map on A. The user wants to
        # keep watching the manually-flown gold-pot view, so we push the
        # coordinate to the device directly and leave engine.current_position
        # untouched. iPhone GPS still flips to A; only the desktop camera
        # stays put.
        if self.engine.state not in (SimulationState.IDLE, SimulationState.DISCONNECTED):
            await self.engine.stop()
        await self.engine.location_service.set(lat, lng)
        await self.engine._emit("goldditto_cycle", {
            "phase": "teleported",
            "lat": lat,
            "lng": lng,
        })
        await self.engine.restore()
        await self.engine._emit("goldditto_cycle", {"phase": "restored"})
        logger.info("Gold Ditto cycle done at (%.6f, %.6f)", lat, lng)

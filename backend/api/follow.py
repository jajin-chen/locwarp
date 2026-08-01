"""Follower feed (/ws/follow) — one-way position stream for companion apps.

VirtualRun (Android) connects here to mirror the primary iOS device's
simulated movement. This module is a READ-ONLY tap on the engine event
stream: it must never raise into the caller and never touch the iOS
simulation pipeline. Protocol spec:
docs/superpowers/specs/2026-08-01-virtualrun-follow-design.md
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter(tags=["follow"])
logger = logging.getLogger(__name__)

PROTOCOL_VERSION = 1

# Live follower connections. Guarded by the event loop (all access is from
# async handlers on the same loop), so a plain list is race-free here.
_followers: list[WebSocket] = []

_COORD_EVENTS = {"position_update": "position", "teleport": "teleport"}


def translate(event_type: str, data: dict, primary_udid: str | None) -> dict | None:
    """Map an internal engine event to a follower protocol message.

    Returns None for anything that must not reach followers: events from
    non-primary devices, unrelated event types, or malformed payloads.
    """
    if not isinstance(data, dict) or primary_udid is None:
        return None
    if data.get("udid") != primary_udid:
        return None
    if event_type in _COORD_EVENTS:
        lat, lng = data.get("lat"), data.get("lng")
        if not isinstance(lat, (int, float)) or not isinstance(lng, (int, float)):
            return None
        return {"type": _COORD_EVENTS[event_type], "lat": float(lat), "lng": float(lng)}
    if event_type == "state_change":
        state = data.get("state")
        if not isinstance(state, str):
            return None
        return {"type": "sim_state", "state": state}
    return None


def hello(primary_udid: str | None, version: str) -> dict:
    """Handshake message sent once on connect."""
    return {
        "type": "hello",
        "app": "locwarp",
        "protocol": PROTOCOL_VERSION,
        "version": version,
        "udid": primary_udid,
    }


async def forward(event_type: str, data: dict) -> None:
    """Fan one engine event out to all followers. Never raises.

    Dead connections are dropped on first send failure — no retry, no
    blocking, so a wedged follower can't slow the iOS pipeline.
    """
    if not _followers:
        return
    try:
        from main import app_state

        msg = translate(event_type, data, app_state._primary_udid)
    except Exception:
        logger.debug("follow translate failed (ignored)", exc_info=True)
        return
    if msg is None:
        return
    text = json.dumps(msg)
    dead = []
    for ws in list(_followers):
        try:
            await ws.send_text(text)
        except Exception:
            dead.append(ws)
    for ws in dead:
        if ws in _followers:
            _followers.remove(ws)
        logger.info("Follower dropped (%d remaining)", len(_followers))

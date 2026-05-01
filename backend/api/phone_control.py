"""Phone-control web UI: lets a phone on the same WiFi reach a small
mobile-friendly page hosted by LocWarp and operate the primary device.

Auth model:
  * Backend generates a 32-hex `token` and a 6-digit `pin` at startup
    (and on every `/rotate` call).
  * Phone opens `http://<lan-ip>:<port>/phone`, types the PIN, and the
    page POSTs the PIN to `/api/phone/auth` to receive the token in
    JSON. The token is stored in localStorage for subsequent reloads.
  * Every action endpoint requires the token via `X-LocWarp-Token`
    header or `?t=` query param.
  * `/api/phone/info` and `/api/phone/rotate` are localhost-only so the
    desktop UI can fetch the URL / PIN without exposing them to LAN.

Earlier revisions also offered a QR-based pairing path that embedded the
token in the URL fragment, but that was removed because anyone with a
camera who glimpsed the screen could pair without typing the PIN. PIN
entry is now the only pairing path.
"""

from __future__ import annotations

import logging
import secrets
import socket
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Header
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

logger = logging.getLogger("locwarp.phone")

router = APIRouter(tags=["phone"])


# ── Auth state ───────────────────────────────────────────────


class _PhoneAuth:
    def __init__(self) -> None:
        self.token: str = secrets.token_hex(16)  # 32 hex chars
        self.pin: str = f"{secrets.randbelow(1_000_000):06d}"
        self.created_at: float = time.monotonic()

    def rotate(self) -> None:
        self.token = secrets.token_hex(16)
        self.pin = f"{secrets.randbelow(1_000_000):06d}"
        self.created_at = time.monotonic()


_auth = _PhoneAuth()


def _check_token(token: str | None) -> None:
    if not token or not secrets.compare_digest(token, _auth.token):
        raise HTTPException(status_code=401, detail={"code": "phone_auth_required",
                                                     "message": "Invalid or missing token"})


def _resolve_token(request: Request, header_token: str | None, query_token: str | None) -> str | None:
    """Pick the token from header / query — header wins to keep a clean URL bar."""
    return header_token or query_token


def _is_localhost(request: Request) -> bool:
    host = (request.client.host if request.client else "") or ""
    # IPv4 loopback or IPv6 loopback
    return host in ("127.0.0.1", "::1", "localhost")


# ── LAN discovery ────────────────────────────────────────────


def _lan_ipv4_candidates() -> list[str]:
    """Best-effort enumeration of IPv4 addresses other devices on the
    same LAN can reach. We try two probes:
      (a) Open a UDP socket and 'connect' to a public IP — Windows /
          Linux fills in the source IP without sending a packet, which
          is the route most LAN devices will see.
      (b) gethostbyname_ex(hostname) — picks up additional NICs on
          machines with multiple adapters (e.g. Ethernet + WiFi).
    Loopback (127.x) is excluded.
    """
    ips: list[str] = []
    seen: set[str] = set()

    def add(ip: str) -> None:
        if not ip or ip.startswith("127."):
            return
        if ip in seen:
            return
        seen.add(ip)
        ips.append(ip)

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(0.5)
            s.connect(("8.8.8.8", 80))
            add(s.getsockname()[0])
    except Exception:
        logger.debug("UDP-connect IP probe failed", exc_info=True)

    try:
        host = socket.gethostname()
        _, _, addrs = socket.gethostbyname_ex(host)
        for a in addrs:
            add(a)
    except Exception:
        logger.debug("gethostbyname_ex probe failed", exc_info=True)

    return ips


# ── Models ───────────────────────────────────────────────────


class _AuthRequest(BaseModel):
    pin: str = Field(min_length=6, max_length=6)


class _TeleportBody(BaseModel):
    lat: float = Field(ge=-90.0, le=90.0)
    lng: float = Field(ge=-180.0, le=180.0)


class _NavigateBody(BaseModel):
    lat: float = Field(ge=-90.0, le=90.0)
    lng: float = Field(ge=-180.0, le=180.0)
    mode: str = "walking"  # walking / running / driving
    # Optional: explicit speed in km/h, overrides the mode preset.
    speed_kmh: float | None = Field(default=None, ge=0.1, le=300.0)


# ── Pairing endpoints (localhost-only or PIN-gated) ──────────


@router.get("/api/phone/info")
async def phone_info(request: Request):
    """Desktop-only: returns LAN IPs, port, and PIN. Used by the desktop
    UI to render a URL + PIN pairing modal."""
    if not _is_localhost(request):
        raise HTTPException(status_code=403, detail="Localhost only")
    from config import API_PORT
    ips = _lan_ipv4_candidates()
    return {
        "port": API_PORT,
        "lan_ips": ips,
        "pin": _auth.pin,
    }


@router.post("/api/phone/rotate")
async def phone_rotate(request: Request):
    """Desktop-only: regenerates PIN + token, invalidating any previously
    paired phone. Use after suspecting compromise or just to refresh."""
    if not _is_localhost(request):
        raise HTTPException(status_code=403, detail="Localhost only")
    _auth.rotate()
    logger.info("Phone-control auth rotated")
    return {"status": "ok"}


@router.post("/api/phone/auth")
async def phone_auth(req: _AuthRequest):
    """PIN-only flow: phone POSTs the PIN it sees on the desktop screen
    and gets the token back. PIN comparison is constant-time."""
    if not secrets.compare_digest(req.pin, _auth.pin):
        raise HTTPException(status_code=401, detail={"code": "bad_pin", "message": "Invalid PIN"})
    return {"token": _auth.token}


# ── Phone-side action endpoints (token required) ─────────────


def _engine():
    """Return the primary simulation engine, or 503 if no device.
    We deliberately avoid the heavyweight rebuild path used by
    api/location.py so phone callers always get a fast, predictable
    answer (the desktop UI is responsible for re-pairing devices)."""
    from main import app_state
    eng = app_state.simulation_engine
    if eng is None:
        raise HTTPException(status_code=503, detail={"code": "no_device",
                                                     "message": "尚未連接 iOS 裝置"})
    return eng


def _all_engines():
    """Return every connected simulation engine. Used so phone-control
    actions fan out to both devices in dual-device group mode (matching
    the desktop UI's behaviour). Falls back to just the primary when
    only one device is connected."""
    from main import app_state
    if not app_state.simulation_engines:
        raise HTTPException(status_code=503, detail={"code": "no_device",
                                                     "message": "尚未連接 iOS 裝置"})
    return list(app_state.simulation_engines.values())


async def _fanout(action_name: str, fn):
    """Run `fn(engine)` on every connected engine concurrently. Logs
    per-engine failures but doesn't bubble them up unless every engine
    failed — that way unplugging one device mid-action still lets the
    other device complete the action."""
    import asyncio
    engines = _all_engines()
    results = await asyncio.gather(
        *[fn(e) for e in engines], return_exceptions=True
    )
    fails = [r for r in results if isinstance(r, Exception)]
    if fails and len(fails) == len(results):
        # Every engine failed — surface the first error so the phone
        # gets a meaningful message instead of a silent success.
        first = fails[0]
        if isinstance(first, HTTPException):
            raise first
        logger.exception("phone %s failed on every engine", action_name, exc_info=first)
        raise HTTPException(status_code=500, detail=str(first))
    if fails:
        logger.warning("phone %s: %d/%d engines failed", action_name, len(fails), len(results))


@router.get("/api/phone/status")
async def phone_status(
    request: Request,
    x_locwarp_token: str | None = Header(default=None, alias="X-LocWarp-Token"),
    t: str | None = None,
):
    _check_token(_resolve_token(request, x_locwarp_token, t))
    from main import app_state
    dm = app_state.device_manager
    eng = app_state.simulation_engine
    devices_info = []
    try:
        for udid, conn in dm._connections.items():
            devices_info.append({
                "udid": udid,
                "name": getattr(conn, "name", "") or "",
                "connection_type": getattr(conn, "connection_type", "USB"),
            })
    except Exception:
        logger.debug("status: device enumeration failed", exc_info=True)

    if eng is None:
        return {
            "connected": False,
            "devices": devices_info,
            "state": "disconnected",
            "current_position": None,
            "route_path": None,
        }
    s = eng.get_status()
    pos = None
    if s.current_position is not None:
        pos = {"lat": s.current_position.lat, "lng": s.current_position.lng}
    route_path = getattr(eng, "_last_route_path", None)
    return {
        "connected": True,
        "devices": devices_info,
        "state": s.state.value if s.state else "idle",
        "current_position": pos,
        "route_path": route_path,
    }


@router.post("/api/phone/teleport")
async def phone_teleport(
    body: _TeleportBody,
    request: Request,
    x_locwarp_token: str | None = Header(default=None, alias="X-LocWarp-Token"),
    t: str | None = None,
):
    """Teleport every connected device to the same coordinate. In single
    device mode this is just one engine; in dual-device group mode both
    iPhones move together, matching the desktop UI's behaviour."""
    _check_token(_resolve_token(request, x_locwarp_token, t))
    await _fanout("teleport", lambda e: e.teleport(body.lat, body.lng))
    return {"status": "ok", "lat": body.lat, "lng": body.lng}


@router.post("/api/phone/stop")
async def phone_stop(
    request: Request,
    x_locwarp_token: str | None = Header(default=None, alias="X-LocWarp-Token"),
    t: str | None = None,
):
    _check_token(_resolve_token(request, x_locwarp_token, t))
    await _fanout("stop", lambda e: e.stop())
    return {"status": "stopped"}


@router.post("/api/phone/restore")
async def phone_restore(
    request: Request,
    x_locwarp_token: str | None = Header(default=None, alias="X-LocWarp-Token"),
    t: str | None = None,
):
    _check_token(_resolve_token(request, x_locwarp_token, t))
    await _fanout("restore", lambda e: e.restore())
    return {"status": "restored"}


@router.post("/api/phone/navigate")
async def phone_navigate(
    body: _NavigateBody,
    request: Request,
    x_locwarp_token: str | None = Header(default=None, alias="X-LocWarp-Token"),
    t: str | None = None,
):
    """Navigate (walk / drive) from the current virtual position to the
    given coordinate. Spawns the simulation in the background so the
    HTTP call returns quickly. Refuses with 400 if there's no virtual
    origin yet — without one the engine has nothing to interpolate from
    and the sim would silently no-op, which the phone UI used to mistake
    for a successful start."""
    _check_token(_resolve_token(request, x_locwarp_token, t))
    eng = _engine()
    if eng.current_position is None:
        raise HTTPException(status_code=400, detail={
            "code": "no_position",
            "message": "尚未有虛擬位置,請先瞬移或飛座標",
        })
    from models.schemas import Coordinate, MovementMode
    try:
        mode = MovementMode(body.mode)
    except ValueError:
        mode = MovementMode.WALKING
    import asyncio
    asyncio.create_task(eng.navigate(
        Coordinate(lat=body.lat, lng=body.lng),
        mode,
        speed_kmh=body.speed_kmh,
    ))
    return {
        "status": "started",
        "destination": {"lat": body.lat, "lng": body.lng},
        "mode": mode.value,
        "speed_kmh": body.speed_kmh,
    }


@router.get("/api/phone/geocode")
async def phone_geocode(
    request: Request,
    q: str,
    x_locwarp_token: str | None = Header(default=None, alias="X-LocWarp-Token"),
    t: str | None = None,
):
    """Forward geocode via the existing GeocodingService (Nominatim).
    Returned as a list of {display_name, short_name, lat, lng,
    country_code} so the phone can render a results list."""
    _check_token(_resolve_token(request, x_locwarp_token, t))
    from services.geocoding import GeocodingService
    svc = GeocodingService()
    try:
        results = await svc.search(q, limit=8, provider="nominatim", google_key=None)
    except Exception as e:
        logger.exception("phone geocode failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    return [
        {
            "display_name": r.display_name,
            "short_name": r.short_name or r.display_name,
            "lat": r.lat,
            "lng": r.lng,
            "country_code": r.country_code,
        }
        for r in results
    ]


# ── Mobile page ──────────────────────────────────────────────


def _phone_page_path() -> Path:
    """Resolve phone.html in both dev (./backend/static/phone.html) and
    PyInstaller-packaged (sys._MEIPASS/static/phone.html) layouts."""
    import sys
    candidates: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "static" / "phone.html")
    candidates.append(Path(__file__).resolve().parent.parent / "static" / "phone.html")
    for c in candidates:
        if c.exists():
            return c
    return candidates[-1]


@router.get("/phone", response_class=HTMLResponse)
async def phone_page():
    """Serve the embedded mobile control page. Token is read by the
    page JS from `window.location.hash` (#t=...) so the server never
    sees it in transit."""
    path = _phone_page_path()
    try:
        return HTMLResponse(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.error("phone.html missing at %s", path)
        raise HTTPException(status_code=500, detail="phone.html missing")

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import API_HOST, API_PORT, SETTINGS_FILE, DEFAULT_LOCATION
from core.device_manager import DeviceManager
from services.cooldown import CooldownTimer
from services.bookmarks import BookmarkManager
from services.coord_format import CoordinateFormatter
from services.reconnect import ReconnectManager

# Configure logging — console + rotating file in ~/.locwarp/logs/
_log_fmt = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
_log_dir = Path.home() / ".locwarp" / "logs"
try:
    _log_dir.mkdir(parents=True, exist_ok=True)
    _file_handler = RotatingFileHandler(
        _log_dir / "backend.log",
        maxBytes=2 * 1024 * 1024,  # 2 MB
        backupCount=3,
        encoding="utf-8",
    )
    _file_handler.setFormatter(logging.Formatter(_log_fmt))
    _file_handler.setLevel(logging.INFO)
    _handlers = [logging.StreamHandler(), _file_handler]
except Exception:
    _handlers = [logging.StreamHandler()]
logging.basicConfig(level=logging.INFO, format=_log_fmt, handlers=_handlers, force=True)
logger = logging.getLogger("locwarp")


class AppState:
    """Central application state — shared across API endpoints."""

    def __init__(self):
        self.device_manager = DeviceManager()
        self.simulation_engine = None  # Created when a device connects
        self.cooldown_timer = CooldownTimer()
        self.bookmark_manager = BookmarkManager()
        self.coord_formatter = CoordinateFormatter()
        self.reconnect_manager = None
        self._last_position = None
        self._load_settings()

    def _load_settings(self):
        if SETTINGS_FILE.exists():
            try:
                data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
                pos = data.get("last_position")
                if pos:
                    self._last_position = pos
                fmt = data.get("coord_format")
                if fmt:
                    from models.schemas import CoordinateFormat
                    self.coord_formatter.format = CoordinateFormat(fmt)
                cd = data.get("cooldown_enabled")
                if cd is not None:
                    self.cooldown_timer.enabled = cd
            except (json.JSONDecodeError, OSError, ValueError, KeyError):
                logger.warning("Settings file malformed or unreadable; using defaults", exc_info=True)

    def save_settings(self):
        data = {
            "last_position": self._last_position,
            "coord_format": self.coord_formatter.format.value,
            "cooldown_enabled": self.cooldown_timer.enabled,
        }
        try:
            SETTINGS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning("Failed to save settings: %s", e)

    def get_initial_position(self) -> dict:
        if self._last_position:
            return self._last_position
        # Could try IP geolocation here; fallback to default
        return DEFAULT_LOCATION

    def update_last_position(self, lat: float, lng: float):
        self._last_position = {"lat": lat, "lng": lng}

    async def create_engine_for_device(self, udid: str):
        """Create a SimulationEngine for the connected device."""
        from core.simulation_engine import SimulationEngine
        from api.websocket import broadcast

        loc_service = await self.device_manager.get_location_service(udid)

        async def event_callback(event_type: str, data: dict):
            await broadcast(event_type, data)
            if event_type == "position_update" and "lat" in data:
                self.update_last_position(data["lat"], data["lng"])

        self.simulation_engine = SimulationEngine(loc_service, event_callback)

        # Set initial position and push to device
        init = self.get_initial_position()
        from models.schemas import Coordinate
        self.simulation_engine.current_position = Coordinate(
            lat=init["lat"], lng=init["lng"]
        )

        # Actually simulate the position on the device
        try:
            await loc_service.set(init["lat"], init["lng"])
            logger.info("Initial position set on device: (%.6f, %.6f)", init["lat"], init["lng"])
        except Exception:
            logger.exception("Failed to push initial position to device")

        # Setup reconnect manager
        self.reconnect_manager = ReconnectManager(self.device_manager)

        logger.info("Simulation engine created for device %s", udid)


app_state = AppState()


# ── Lifespan ─────────────────────────────────────────────

@asynccontextmanager
async def lifespan(application: FastAPI):
    # ── Startup ──
    logger.info("LocWarp starting — scanning for devices…")
    try:
        devices = await app_state.device_manager.discover_devices()
        if devices:
            target = devices[0]
            logger.info("Found device %s (%s), auto-connecting…", target.name, target.udid)
            await app_state.device_manager.connect(target.udid)
            await app_state.create_engine_for_device(target.udid)
            logger.info("Auto-connected to %s", target.udid)
        else:
            logger.info("No iOS devices found on startup")
    except Exception:
        logger.exception("Auto-connect on startup failed (device may need manual connect)")

    yield

    # ── Shutdown ──
    app_state.save_settings()
    await app_state.device_manager.disconnect_all()
    logger.info("LocWarp shut down")


# ── FastAPI app ───────────────────────────────────────────

app = FastAPI(title="LocWarp", version="0.1.0", description="iOS Virtual Location Simulator", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routers
from api.device import router as device_router
from api.location import router as location_router
from api.route import router as route_router
from api.geocode import router as geocode_router
from api.bookmarks import router as bookmarks_router
from api.websocket import router as ws_router

app.include_router(device_router)
app.include_router(location_router)
app.include_router(route_router)
app.include_router(geocode_router)
app.include_router(bookmarks_router)
app.include_router(ws_router)


@app.get("/")
async def root():
    return {
        "name": "LocWarp",
        "version": "0.1.0",
        "status": "running",
        "initial_position": app_state.get_initial_position(),
    }



if __name__ == "__main__":
    uvicorn.run("main:app", host=API_HOST, port=API_PORT, reload=False)

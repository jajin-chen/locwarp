# VirtualRun Follow Mode — LocWarp Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a one-way `/ws/follow` WebSocket feed + mDNS advertisement so the VirtualRun Android app can mirror the primary iOS device's simulated movement.

**Architecture:** A new `backend/api/follow.py` taps the existing engine event stream (the `event_callback` in `main.py`) read-only, translates `position_update` / `teleport` / `state_change` events for the primary udid into a small versioned JSON protocol, and pushes them to follower WebSocket connections. A new `backend/services/follow_discovery.py` advertises `_locwarp-follow._tcp.local.` via zeroconf. **Nothing in the iOS simulation pipeline may be modified or affected** — all new code paths are wrapped so any failure is logged and swallowed.

**Tech Stack:** Python 3.11+, FastAPI WebSockets, zeroconf (already a transitive dep of pymobiledevice3), pytest + pytest-asyncio (`asyncio_mode = auto` in `backend/pytest.ini`).

**Spec:** `docs/superpowers/specs/2026-08-01-virtualrun-follow-design.md`

## Global Constraints

- **絕不能影響既有 iOS 模擬功能**:不修改 `simulation_engine.py`、`location_service.py`、`device_manager.py`、`api/websocket.py` 的既有邏輯;`main.py` 只允許在 `event_callback` 加一段 guarded hook、在 `lifespan` 加 guarded mDNS 註冊/註銷、註冊一個新 router。
- Protocol version = `1`。Server→Client 訊息只有四種:`hello` / `position` / `teleport` / `sim_state`。Client→Server 不定義任何指令(收到一律忽略)。
- 只轉發 primary udid 的事件;非 primary 一律丟棄。
- mDNS service type = `_locwarp-follow._tcp.local.`,TXT 帶 `protocol=1`、`version`、`path=/ws/follow`;註冊失敗只記 warning,絕不阻擋啟動。
- 所有測試指令在 `backend/` 目錄下執行:`python -m pytest tests/... -v`(Windows;先 `pip install -r requirements-dev.txt`)。
- Work on branch `feat/virtualrun-follow`(已存在,基於 `feat/reconnect-discovery`)。

---

### Task 1: Follower protocol core (`translate` / `hello` / `forward`)

**Files:**
- Create: `backend/api/follow.py`
- Test: `backend/tests/test_follow_protocol.py`

**Interfaces:**
- Consumes: nothing new (pure module + `main.app_state._primary_udid` read lazily inside `forward`).
- Produces (used by Task 2 and 3):
  - `translate(event_type: str, data: dict, primary_udid: str | None) -> dict | None`
  - `hello(primary_udid: str | None, version: str) -> dict`
  - `async forward(event_type: str, data: dict) -> None` — never raises
  - `PROTOCOL_VERSION: int = 1`
  - `_followers: list` — module-level registry of live follower WebSockets

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_follow_protocol.py`:

```python
"""Unit tests for the /ws/follow protocol translation and fan-out.

The follower feed is a read-only tap on the engine event stream — these
tests pin the translation rules (event filtering, udid gating) and the
never-raise guarantee of forward().
"""

from __future__ import annotations

import json

import pytest

from api import follow

PRIMARY = "PRIMARY-UDID"


# ── translate ────────────────────────────────────────────

def test_position_update_translates_to_position():
    msg = follow.translate(
        "position_update", {"lat": 25.03, "lng": 121.56, "udid": PRIMARY}, PRIMARY
    )
    assert msg == {"type": "position", "lat": 25.03, "lng": 121.56}


def test_teleport_translates_to_teleport():
    msg = follow.translate(
        "teleport", {"lat": 24.0, "lng": 120.0, "udid": PRIMARY}, PRIMARY
    )
    assert msg == {"type": "teleport", "lat": 24.0, "lng": 120.0}


def test_state_change_translates_to_sim_state():
    msg = follow.translate(
        "state_change", {"state": "navigating", "udid": PRIMARY}, PRIMARY
    )
    assert msg == {"type": "sim_state", "state": "navigating"}


def test_non_primary_udid_is_dropped():
    msg = follow.translate(
        "position_update", {"lat": 25.0, "lng": 121.0, "udid": "OTHER"}, PRIMARY
    )
    assert msg is None


def test_no_primary_device_drops_everything():
    msg = follow.translate(
        "position_update", {"lat": 25.0, "lng": 121.0, "udid": PRIMARY}, None
    )
    assert msg is None


def test_unrelated_events_are_dropped():
    for evt in ("route_path", "lap_complete", "ddi_mounted", "goldditto_cycle"):
        assert follow.translate(evt, {"udid": PRIMARY, "lat": 1.0, "lng": 2.0}, PRIMARY) is None


def test_position_with_missing_or_bad_coords_is_dropped():
    assert follow.translate("position_update", {"udid": PRIMARY}, PRIMARY) is None
    assert follow.translate(
        "position_update", {"lat": "bad", "lng": 121.0, "udid": PRIMARY}, PRIMARY
    ) is None


def test_state_change_with_non_string_state_is_dropped():
    assert follow.translate("state_change", {"state": 7, "udid": PRIMARY}, PRIMARY) is None


def test_non_dict_data_is_dropped():
    assert follow.translate("position_update", None, PRIMARY) is None  # type: ignore[arg-type]


# ── hello ────────────────────────────────────────────────

def test_hello_shape():
    msg = follow.hello(PRIMARY, "0.1.0")
    assert msg == {
        "type": "hello",
        "app": "locwarp",
        "protocol": follow.PROTOCOL_VERSION,
        "version": "0.1.0",
        "udid": PRIMARY,
    }
    assert follow.hello(None, "0.1.0")["udid"] is None


# ── forward ──────────────────────────────────────────────

class FakeFollowerWs:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.sent: list[str] = []

    async def send_text(self, text: str) -> None:
        if self.fail:
            raise RuntimeError("socket dead")
        self.sent.append(text)


@pytest.fixture
def primary_engine(monkeypatch):
    """Point app_state at a primary udid without touching real hardware."""
    from main import app_state

    monkeypatch.setattr(app_state, "_primary_udid", PRIMARY)
    return app_state


@pytest.fixture(autouse=True)
def clean_followers():
    follow._followers.clear()
    yield
    follow._followers.clear()


async def test_forward_sends_to_all_followers(primary_engine):
    a, b = FakeFollowerWs(), FakeFollowerWs()
    follow._followers.extend([a, b])
    await follow.forward("position_update", {"lat": 25.0, "lng": 121.0, "udid": PRIMARY})
    assert json.loads(a.sent[0]) == {"type": "position", "lat": 25.0, "lng": 121.0}
    assert json.loads(b.sent[0]) == {"type": "position", "lat": 25.0, "lng": 121.0}


async def test_forward_removes_dead_follower_and_keeps_serving(primary_engine):
    dead, alive = FakeFollowerWs(fail=True), FakeFollowerWs()
    follow._followers.extend([dead, alive])
    await follow.forward("position_update", {"lat": 25.0, "lng": 121.0, "udid": PRIMARY})
    assert dead not in follow._followers
    assert alive in follow._followers
    assert len(alive.sent) == 1


async def test_forward_never_raises_even_if_translate_blows_up(monkeypatch, primary_engine):
    follow._followers.append(FakeFollowerWs())

    def boom(*a, **k):
        raise RuntimeError("translate bug")

    monkeypatch.setattr(follow, "translate", boom)
    await follow.forward("position_update", {"lat": 25.0, "lng": 121.0, "udid": PRIMARY})
    # reaching here without an exception IS the assertion


async def test_forward_noop_with_no_followers(primary_engine):
    await follow.forward("position_update", {"lat": 25.0, "lng": 121.0, "udid": PRIMARY})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_follow_protocol.py -v` (in `backend/`)
Expected: FAIL — `ModuleNotFoundError: No module named 'api.follow'` (collection error is fine).

- [ ] **Step 3: Write the implementation**

Create `backend/api/follow.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_follow_protocol.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/api/follow.py backend/tests/test_follow_protocol.py
git commit -m "feat: add follower protocol core (translate/hello/forward) for /ws/follow"
```

---

### Task 2: `/ws/follow` endpoint + event_callback hook + isolation regression test

**Files:**
- Modify: `backend/api/follow.py` (append the WebSocket endpoint)
- Modify: `backend/main.py` — `event_callback` inside `create_engine_for_device` (around line 176–182) and router registration block (around line 694–713)
- Test: `backend/tests/test_follow_endpoint.py`

**Interfaces:**
- Consumes: `follow.hello`, `follow._followers`, `follow.forward` from Task 1.
- Produces: WebSocket route `GET /ws/follow` (accepts, sends `hello`, then streams); `main.event_callback` now calls `follow.forward` guarded — the hook Task 3 does NOT touch.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_follow_endpoint.py`:

```python
"""Endpoint + isolation tests for /ws/follow.

The isolation test is the contract that matters most: a blowing-up
follower pipeline must NEVER break the iOS event pipeline
(broadcast + update_last_position).
"""

from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

from api import follow
from fakes import FakeLocationService

TEST_UDID = "TEST-UDID-0001"


@pytest.fixture(autouse=True)
def clean_followers():
    follow._followers.clear()
    yield
    follow._followers.clear()


@pytest.fixture
def primary(monkeypatch):
    from main import app_state

    monkeypatch.setattr(app_state, "_primary_udid", TEST_UDID)
    return app_state


def test_ws_follow_sends_hello_on_connect(primary):
    from main import app

    # NOTE: deliberately NOT `with TestClient(app)` — entering the client
    # context runs the lifespan (real device discovery). websocket_connect
    # alone skips lifespan, same spirit as the ASGITransport smoke tests.
    client = TestClient(app)
    with client.websocket_connect("/ws/follow") as ws:
        msg = json.loads(ws.receive_text())
        assert msg["type"] == "hello"
        assert msg["app"] == "locwarp"
        assert msg["protocol"] == 1
        assert msg["udid"] == TEST_UDID


def test_ws_follow_ignores_inbound_text(primary):
    from main import app

    client = TestClient(app)
    with client.websocket_connect("/ws/follow") as ws:
        ws.receive_text()  # hello
        ws.send_text('{"type":"evil_command"}')  # must be ignored, not crash
        ws.send_text("not json at all")
    # surviving the context exit without server error IS the assertion


async def test_engine_event_reaches_follower(monkeypatch, primary):
    """End-to-end: engine _emit → event_callback → forward → follower ws."""
    from main import app_state

    sent: list[tuple[str, dict]] = []

    async def fake_broadcast(event_type, data):
        sent.append((event_type, data))

    monkeypatch.setattr("api.websocket.broadcast", fake_broadcast)

    async def fake_get_location_service(udid):
        return FakeLocationService()

    monkeypatch.setattr(
        app_state.device_manager, "get_location_service", fake_get_location_service
    )
    app_state.simulation_engines.pop(TEST_UDID, None)
    await app_state.create_engine_for_device(TEST_UDID)
    engine = app_state.simulation_engines[TEST_UDID]

    class FakeWs:
        def __init__(self):
            self.sent = []

        async def send_text(self, text):
            self.sent.append(text)

    fw = FakeWs()
    follow._followers.append(fw)

    await engine._emit("position_update", {"lat": 25.0, "lng": 121.0})

    assert json.loads(fw.sent[0]) == {"type": "position", "lat": 25.0, "lng": 121.0}
    app_state.simulation_engines.pop(TEST_UDID, None)


async def test_follow_failure_never_breaks_ios_pipeline(monkeypatch, primary):
    """THE isolation regression test: follow.forward raising must not stop
    broadcast nor update_last_position."""
    from main import app_state

    sent: list[tuple[str, dict]] = []

    async def fake_broadcast(event_type, data):
        sent.append((event_type, data))

    monkeypatch.setattr("api.websocket.broadcast", fake_broadcast)

    async def bomb(event_type, data):
        raise RuntimeError("follower pipeline is on fire")

    monkeypatch.setattr(follow, "forward", bomb)

    async def fake_get_location_service(udid):
        return FakeLocationService()

    monkeypatch.setattr(
        app_state.device_manager, "get_location_service", fake_get_location_service
    )
    app_state.simulation_engines.pop(TEST_UDID, None)
    await app_state.create_engine_for_device(TEST_UDID)
    engine = app_state.simulation_engines[TEST_UDID]

    await engine._emit("position_update", {"lat": 25.5, "lng": 121.5})

    assert sent, "broadcast must still run when follow.forward raises"
    assert sent[0][0] == "position_update"
    assert app_state._last_position == {"lat": 25.5, "lng": 121.5}
    app_state.simulation_engines.pop(TEST_UDID, None)
```

Note: check how `update_last_position` stores the value (`backend/main.py:126`) — if it stores something other than a `{"lat","lng"}` dict, adjust the last assertion to match the real shape before committing the test.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_follow_endpoint.py -v`
Expected: FAIL — `/ws/follow` route does not exist (403/404 on websocket connect), and the isolation test fails because `event_callback` doesn't call `follow.forward` yet.

- [ ] **Step 3: Implement the endpoint**

Append to `backend/api/follow.py`:

```python
@router.websocket("/ws/follow")
async def follow_endpoint(ws: WebSocket):
    """One-way follower feed. Sends hello, then streams translated events
    pushed by forward(). Inbound text is ignored (protocol is one-way)."""
    await ws.accept()
    try:
        from main import app, app_state

        await ws.send_text(json.dumps(hello(app_state._primary_udid, app.version)))
    except Exception:
        logger.debug("follow hello failed; closing", exc_info=True)
        return
    _followers.append(ws)
    logger.info("Follower connected (%d total)", len(_followers))
    try:
        while True:
            await ws.receive_text()  # one-way protocol: drain and ignore
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.debug("follower socket error (ignored)", exc_info=True)
    finally:
        if ws in _followers:
            _followers.remove(ws)
        logger.info("Follower disconnected (%d remaining)", len(_followers))
```

- [ ] **Step 4: Wire the router and the guarded hook in `main.py`**

In the router registration block (after `from api.phone_control import router as phone_router`, around `backend/main.py:703`):

```python
from api.follow import router as follow_router
```

and after `app.include_router(phone_router)`:

```python
app.include_router(follow_router)
```

In `create_engine_for_device`'s `event_callback` (around `backend/main.py:176`), change:

```python
        async def event_callback(event_type: str, data: dict):
            # Always tag emissions with udid so the frontend can route per-device.
            if isinstance(data, dict) and "udid" not in data:
                data = {**data, "udid": udid}
            await broadcast(event_type, data)
            if event_type == "position_update" and "lat" in data:
                self.update_last_position(data["lat"], data["lng"])
```

to:

```python
        async def event_callback(event_type: str, data: dict):
            # Always tag emissions with udid so the frontend can route per-device.
            if isinstance(data, dict) and "udid" not in data:
                data = {**data, "udid": udid}
            await broadcast(event_type, data)
            if event_type == "position_update" and "lat" in data:
                self.update_last_position(data["lat"], data["lng"])
            # Follower feed (VirtualRun) — read-only tap; a broken follower
            # pipeline must never take down the iOS event pipeline.
            try:
                from api import follow
                await follow.forward(event_type, data)
            except Exception:
                logger.debug("follow forward error (ignored)", exc_info=True)
```

(`follow.forward` is looked up through the module at call time — not imported as a bare name — so tests can monkeypatch `follow.forward`.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_follow_endpoint.py tests/test_follow_protocol.py -v`
Expected: all PASS.

- [ ] **Step 6: Run the full backend suite to prove nothing else broke**

Run: `python -m pytest tests -v`
Expected: all PASS (same pass count as before this task, plus the new tests).

- [ ] **Step 7: Commit**

```bash
git add backend/api/follow.py backend/main.py backend/tests/test_follow_endpoint.py
git commit -m "feat: expose /ws/follow follower endpoint with guarded event tap"
```

---

### Task 3: mDNS advertisement (`_locwarp-follow._tcp.local.`)

**Files:**
- Create: `backend/services/follow_discovery.py`
- Modify: `backend/main.py` — `lifespan` (around lines 646–679)
- Modify: `backend/requirements.txt`
- Test: `backend/tests/test_follow_discovery.py`

**Interfaces:**
- Consumes: `config.API_PORT`.
- Produces: `start_advertise(port: int, version: str) -> None` and `stop_advertise() -> None` — both synchronous, both never raise. `SERVICE_TYPE = "_locwarp-follow._tcp.local."` (the exact string VirtualRun's NsdManager scans for — Android side uses `_locwarp-follow._tcp.` without the `local.` suffix).

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_follow_discovery.py`:

```python
"""Tests for the mDNS advertisement helper.

The only hard contract: start/stop NEVER raise — mDNS failure degrades to
manual-IP pairing, it must not break LocWarp startup/shutdown.
"""

from __future__ import annotations

import socket

from services import follow_discovery


def teardown_function():
    follow_discovery.stop_advertise()  # idempotent cleanup between tests


def test_service_type_matches_spec():
    assert follow_discovery.SERVICE_TYPE == "_locwarp-follow._tcp.local."


def test_local_ipv4s_returns_packed_addresses():
    addrs = follow_discovery._local_ipv4s()
    assert isinstance(addrs, list)
    for a in addrs:
        assert isinstance(a, bytes) and len(a) == 4
        assert not socket.inet_ntoa(a).startswith("127.")


def test_start_advertise_survives_zeroconf_failure(monkeypatch):
    import zeroconf

    def boom(*args, **kwargs):
        raise RuntimeError("no network stack")

    monkeypatch.setattr(zeroconf, "Zeroconf", boom)
    follow_discovery.start_advertise(8777, "0.1.0")  # must not raise
    assert follow_discovery._zc is None


def test_start_advertise_skips_when_no_local_ip(monkeypatch):
    monkeypatch.setattr(follow_discovery, "_local_ipv4s", lambda: [])
    follow_discovery.start_advertise(8777, "0.1.0")  # must not raise
    assert follow_discovery._zc is None


def test_stop_advertise_without_start_is_noop():
    follow_discovery.stop_advertise()  # must not raise


def test_start_and_stop_roundtrip(monkeypatch):
    """Register against a fake Zeroconf to verify the ServiceInfo we build."""
    import zeroconf

    registered = {}

    class FakeZeroconf:
        def register_service(self, info):
            registered["info"] = info

        def unregister_service(self, info):
            registered["unregistered"] = True

        def close(self):
            registered["closed"] = True

    monkeypatch.setattr(zeroconf, "Zeroconf", FakeZeroconf)
    monkeypatch.setattr(
        follow_discovery, "_local_ipv4s", lambda: [socket.inet_aton("192.168.1.10")]
    )
    follow_discovery.start_advertise(8777, "0.2.0")

    info = registered["info"]
    assert info.type == follow_discovery.SERVICE_TYPE
    assert info.port == 8777
    props = {k.decode(): v.decode() for k, v in info.properties.items()}
    assert props["protocol"] == "1"
    assert props["path"] == "/ws/follow"
    assert props["version"] == "0.2.0"

    follow_discovery.stop_advertise()
    assert registered.get("unregistered") is True
    assert registered.get("closed") is True
    assert follow_discovery._zc is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_follow_discovery.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'services.follow_discovery'`.

- [ ] **Step 3: Write the implementation**

Create `backend/services/follow_discovery.py`:

```python
"""mDNS advertisement for the /ws/follow follower feed.

Registers ``_locwarp-follow._tcp.local.`` so VirtualRun (Android
NsdManager) can auto-discover this machine on the LAN. Everything here is
best-effort: registration failure degrades to manual-IP pairing and must
never break LocWarp startup or shutdown.
"""

from __future__ import annotations

import logging
import re
import socket

logger = logging.getLogger(__name__)

SERVICE_TYPE = "_locwarp-follow._tcp.local."

_zc = None
_info = None


def _local_ipv4s() -> list[bytes]:
    """All non-loopback IPv4 addresses of this machine, packed for zeroconf."""
    addrs: set[str] = set()
    try:
        for res in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = res[4][0]
            if not ip.startswith("127."):
                addrs.add(ip)
    except OSError:
        pass
    if not addrs:
        # Fallback: outbound-route trick — no packet is actually sent.
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect(("8.8.8.8", 80))
                addrs.add(s.getsockname()[0])
            finally:
                s.close()
        except OSError:
            pass
    return [socket.inet_aton(a) for a in sorted(addrs)]


def start_advertise(port: int, version: str) -> None:
    """Register the mDNS service. Never raises; failure logs a warning."""
    global _zc, _info
    try:
        from zeroconf import ServiceInfo, Zeroconf

        addresses = _local_ipv4s()
        if not addresses:
            logger.warning("follow mDNS: no local IPv4 found; manual-IP pairing only")
            return
        host = re.sub(r"[^A-Za-z0-9-]", "-", socket.gethostname()) or "PC"
        _info = ServiceInfo(
            SERVICE_TYPE,
            f"LocWarp-{host}.{SERVICE_TYPE}",
            addresses=addresses,
            port=port,
            properties={"protocol": "1", "version": version, "path": "/ws/follow"},
        )
        _zc = Zeroconf()
        _zc.register_service(_info)
        logger.info("follow mDNS registered (%s, port %d)", host, port)
    except Exception:
        _zc = None
        _info = None
        logger.warning(
            "follow mDNS registration failed; manual-IP pairing only", exc_info=True
        )


def stop_advertise() -> None:
    """Unregister and close. Never raises; idempotent."""
    global _zc, _info
    try:
        if _zc is not None:
            if _info is not None:
                _zc.unregister_service(_info)
            _zc.close()
    except Exception:
        logger.debug("follow mDNS unregister failed (ignored)", exc_info=True)
    finally:
        _zc = None
        _info = None
```

Append to `backend/requirements.txt` (zeroconf is already installed transitively via pymobiledevice3 — this pins the direct usage explicitly):

```
zeroconf>=0.132
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_follow_discovery.py -v`
Expected: all PASS.

- [ ] **Step 5: Hook into `lifespan` in `main.py`**

In the Startup section of `lifespan` (after the `keepalive_task` line, around `backend/main.py:664`):

```python
    # Advertise the follower feed on the LAN (best-effort; see follow_discovery).
    try:
        from services import follow_discovery
        await asyncio.to_thread(follow_discovery.start_advertise, API_PORT, application.version)
    except Exception:
        logger.warning("follow mDNS startup failed (ignored)", exc_info=True)
```

In the Shutdown section (before `app_state.save_settings()`, around `backend/main.py:677`):

```python
    try:
        from services import follow_discovery
        await asyncio.to_thread(follow_discovery.stop_advertise)
    except Exception:
        logger.debug("follow mDNS shutdown failed (ignored)", exc_info=True)
```

- [ ] **Step 6: Run the full backend suite + boot smoke check**

Run: `python -m pytest tests -v`
Expected: all PASS.

Then boot the backend for a real smoke check (needs no iPhone):

Run (in `backend/`): `python -c "import main"` — expect no import error.
Optionally run `python main.py` for ~5 seconds and confirm the log shows either `follow mDNS registered` or the warning — then Ctrl+C; either outcome is acceptable, a crash is not.

- [ ] **Step 7: Commit**

```bash
git add backend/services/follow_discovery.py backend/tests/test_follow_discovery.py backend/main.py backend/requirements.txt
git commit -m "feat: advertise _locwarp-follow._tcp via mDNS for VirtualRun auto-discovery"
```

---

## Verification checklist (after all tasks)

- [ ] `python -m pytest tests -v` in `backend/` — everything green.
- [ ] Manual: start LocWarp with an iPhone connected, connect with a WS client (e.g. `npx wscat -c ws://<PC-IP>:8777/ws/follow`), teleport / navigate in the UI, confirm `hello` then a stream of `position` / `teleport` / `sim_state` JSON lines, and confirm the desktop UI + iPhone keep working exactly as before.
- [ ] Manual: kill the WS client mid-simulation — LocWarp log shows `Follower dropped`, simulation unaffected.

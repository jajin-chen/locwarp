"""Phone-control capability URL contract.

The phone page is intentionally reachable without a pairing form: the
capability token is carried in the URL fragment, which browsers do not send
to the server.  The backend still authenticates every phone action with the
token, so copying/opening the complete URL is the only required setup step.
"""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest


@pytest.fixture
def deterministic_phone_nics(monkeypatch):
    """Keep `/api/phone/info` independent of the host running the tests."""
    from api import phone_control

    monkeypatch.setattr(
        phone_control,
        "_enumerate_nics",
        lambda: [
            {
                "ip": "192.168.50.10",
                "iface": "Wi-Fi",
                "kind": "wifi",
                "primary": True,
            }
        ],
    )


@pytest.fixture
async def localhost_client():
    from main import app

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 43123))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        yield client


@pytest.fixture
async def lan_client():
    from main import app

    transport = httpx.ASGITransport(app=app, client=("192.168.50.20", 43124))
    async with httpx.AsyncClient(transport=transport, base_url="http://locwarp-lan") as client:
        yield client


async def _phone_info(client: httpx.AsyncClient) -> dict:
    response = await client.get("/api/phone/info")
    assert response.status_code == 200, response.text
    return response.json()


async def test_localhost_info_exposes_capability_token_without_pin(
    localhost_client: httpx.AsyncClient,
    deterministic_phone_nics,
):
    info = await _phone_info(localhost_client)

    assert re.fullmatch(r"[0-9a-f]{32}", info["token"])
    assert "pin" not in info
    assert info["port"] > 0
    assert info["lan_ips"] == ["192.168.50.10"]


async def test_phone_info_remains_localhost_only(
    lan_client: httpx.AsyncClient,
    deterministic_phone_nics,
):
    response = await lan_client.get("/api/phone/info")

    assert response.status_code == 403


async def test_phone_status_requires_token_and_accepts_capability_token(
    localhost_client: httpx.AsyncClient,
    deterministic_phone_nics,
):
    token = (await _phone_info(localhost_client))["token"]

    missing = await localhost_client.get("/api/phone/status")
    wrong = await localhost_client.get(
        "/api/phone/status",
        headers={"X-LocWarp-Token": "0" * 32},
    )
    correct = await localhost_client.get(
        "/api/phone/status",
        headers={"X-LocWarp-Token": token},
    )

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert correct.status_code == 200


async def test_non_ascii_token_is_an_invalid_token_not_an_internal_error(
    localhost_client: httpx.AsyncClient,
):
    # HTTPX encodes request headers as Latin-1, so exercise the same token
    # resolver through the query-string form.  The endpoint must fail closed
    # with 401 even when compare_digest cannot compare a non-ASCII string.
    response = await localhost_client.get("/api/phone/status?t=%C3%A9")

    assert response.status_code == 401


async def test_phone_rotate_remains_localhost_only(
    lan_client: httpx.AsyncClient,
):
    response = await lan_client.post("/api/phone/rotate")

    assert response.status_code == 403


async def test_phone_mutation_requires_token(localhost_client: httpx.AsyncClient):
    response = await localhost_client.post(
        "/api/phone/teleport",
        json={"lat": 25.0330, "lng": 121.5654},
    )

    assert response.status_code == 401


async def test_rotate_invalidates_old_url_token_and_publishes_new_one(
    localhost_client: httpx.AsyncClient,
    deterministic_phone_nics,
):
    old_token = (await _phone_info(localhost_client))["token"]

    rotated = await localhost_client.post("/api/phone/rotate")
    assert rotated.status_code == 200

    new_token = (await _phone_info(localhost_client))["token"]
    assert new_token != old_token
    assert (
        await localhost_client.get(
            "/api/phone/status",
            headers={"X-LocWarp-Token": old_token},
        )
    ).status_code == 401
    assert (
        await localhost_client.get(
            "/api/phone/status",
            headers={"X-LocWarp-Token": new_token},
        )
    ).status_code == 200


async def test_pin_auth_endpoint_is_removed(localhost_client: httpx.AsyncClient):
    response = await localhost_client.post(
        "/api/phone/auth",
        json={"pin": "000000"},
    )

    assert response.status_code == 404


async def test_phone_page_uses_fragment_token_and_non_pin_invalid_link_gate(
    localhost_client: httpx.AsyncClient,
):
    response = await localhost_client.get("/phone")
    assert response.status_code == 200
    html = response.text

    # The URL fragment is the capability handoff.  It must never regress to
    # a form that asks the user to copy a second credential.
    assert re.search(r"location\.hash", html)
    assert "X-LocWarp-Token" in html
    assert 'id="pin-input"' not in html
    assert "/api/phone/auth" not in html

    # The only expected unauthenticated state is an invalid/incomplete link;
    # it is not a PIN-entry gate.
    assert re.search(r'id=["\']invalid-link["\']', html)
    assert re.search(r"invalid|無效|missing|缺少", html, re.IGNORECASE)


def test_phone_page_reapplies_hash_tokens_and_scrubs_fragment_on_change():
    page = Path(__file__).resolve().parents[1] / "static" / "phone.html"
    html = page.read_text(encoding="utf-8")
    compact = re.sub(r"\s+", " ", html)

    # A phone can remain open while the desktop generates/copies a fresh
    # capability URL.  The page must listen for a new fragment, parse a
    # valid token, install it for subsequent API calls, and poll again.
    listener = re.search(
        r"(?:window\.)?addEventListener\(\s*['\"]hashchange['\"]\s*,",
        compact,
    )
    assert listener, "phone page must react to a newly supplied #t token"
    listener_context = compact[max(0, listener.start() - 1200): listener.end() + 1800]
    assert re.search(r"tokenFromHash\s*\(\)", listener_context)
    assert re.search(r"token\s*=|storeToken\s*\(", listener_context)
    assert re.search(r"pollStatus\s*\(\)", listener_context)

    # Consuming a capability URL should remove the secret from the address
    # bar while retaining the path/query needed for a refresh.
    assert re.search(r"history\.replaceState\s*\(", compact)
    assert re.search(
        r"history\.replaceState\s*\([^)]*(?:location|window\.location)\.(?:pathname|search)",
        compact,
    )


def test_phone_api_snapshots_request_token_and_guards_stale_401_cleanup():
    page = Path(__file__).resolve().parents[1] / "static" / "phone.html"
    html = page.read_text(encoding="utf-8")
    api_match = re.search(
        r"async function api\(path, opts\)\s*\{(?P<body>.*?)"
        r"\n\s*// ── Map ───────────────────────────────────────────────────",
        html,
        re.DOTALL,
    )
    assert api_match, "phone page API helper must remain inspectable as one unit"
    api_source = api_match.group("body")

    # A request must capture the token before awaiting fetch.  Otherwise a
    # hashchange can replace the global token while an older request is still
    # in flight, making the old request send the new credential.
    snapshot = re.search(
        r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*token\s*;",
        api_source,
    )
    assert snapshot, "each phone API request must snapshot the current token"
    snapshot_name = snapshot.group(1)
    header = re.search(
        r"X-LocWarp-Token['\"]\s*:\s*([A-Za-z_$][\w$]*)",
        api_source,
    )
    assert header, "phone API calls must include an auth header"
    assert header.group(1) == snapshot_name

    # A stale 401 from that request may clear/show the gate only if the
    # global token still is the same snapshot.  A newer hashchange token must
    # survive the old request's failure.
    guard = re.search(
        rf"if\s*\(\s*token\s*===\s*{re.escape(snapshot_name)}\s*\)\s*\{{(?P<body>.*?)\}}",
        api_source,
        re.DOTALL,
    )
    assert guard, "401 cleanup must be conditional on the request snapshot"
    guarded_cleanup = guard.group("body")
    assert "token = null" in guarded_cleanup
    assert "clearStoredToken" in guarded_cleanup
    assert "showGate" in guarded_cleanup


def test_phone_page_fixture_is_the_served_page():
    """Keep the static contract test pointed at the file bundled by PyInstaller."""
    page = Path(__file__).resolve().parents[1] / "static" / "phone.html"
    assert page.exists()

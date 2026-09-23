"""Forward / reverse geocoding service.

Forward search supports three providers selected per-request:

* ``nominatim`` — default, the OSM-backed public Nominatim instance. Free,
  no key, but Asian street-level accuracy is weak.
* ``photon`` — komoot-hosted, also OSM-backed, free, no key. Better fuzzy
  matching and typo tolerance than Nominatim, looser rate limits in
  practice. Returns GeoJSON instead of Nominatim's flat shape.
* ``google`` — Google Geocoding API. Requires the user's own API key;
  10k free events / month with the Essentials tier.

Reverse geocoding prefers Nominatim because it feeds country flag +
short-name picking inside the UI, and falls back to Photon when Nominatim
is unavailable.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time

import httpx
from fastapi import HTTPException

from config import NOMINATIM_BASE_URL, NOMINATIM_USER_AGENT, PHOTON_BASE_URL
from models.schemas import GeocodingResult

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
_GOOGLE_GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"

# Lazily-created client shared by every call site so repeated lookups reuse
# TCP+TLS connections instead of handshaking per request. Created on first
# use (never at import time) and kept for the life of the process — fine for
# this desktop app, which has no geocoding-specific shutdown hook.
_client: httpx.AsyncClient | None = None

# The public Nominatim service limits aggregate application traffic to one
# request per second. Reserve request slots across searches and reverse lookups
# so concurrent UI actions cannot exceed that rate.
_NOMINATIM_MIN_INTERVAL = 1.1
_nominatim_request_lock = asyncio.Lock()
_nominatim_last_request_at = 0.0

# A 403 is commonly a temporary service-side block. Stop repeating requests
# during the cooldown and use Photon for both search and reverse lookups.
_nominatim_cooldown_until = 0.0
_nominatim_state_lock = threading.Lock()
_reverse_cache: dict[tuple[float, float], tuple[float, GeocodingResult | None]] = {}
_reverse_cache_lock = threading.Lock()
_reverse_inflight: dict[tuple[float, float], tuple[asyncio.Lock, int]] = {}
_reverse_inflight_lock = threading.Lock()
_REVERSE_CACHE_TTL = 24 * 60 * 60
_REVERSE_NEGATIVE_CACHE_TTL = 30
_REVERSE_CACHE_MAX_ENTRIES = 2048
_CACHE_MISS = object()


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=_TIMEOUT)
    return _client


async def _nominatim_get(
    url: str,
    *,
    params: dict[str, object],
) -> httpx.Response | None:
    """Send a policy-compliant Nominatim request, rate-limited per process."""
    global _nominatim_last_request_at

    async with _nominatim_request_lock:
        with _nominatim_state_lock:
            if time.monotonic() < _nominatim_cooldown_until:
                return None

        delay = _nominatim_last_request_at + _NOMINATIM_MIN_INTERVAL - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)

        with _nominatim_state_lock:
            if time.monotonic() < _nominatim_cooldown_until:
                return None

        _nominatim_last_request_at = time.monotonic()
        try:
            return await _get_client().get(
                url,
                params=params,
                headers={
                    "User-Agent": NOMINATIM_USER_AGENT,
                    "Accept": "application/json",
                },
            )
        except httpx.RequestError:
            _suspend_nominatim(60)
            raise


def _suspend_nominatim(seconds: float) -> None:
    global _nominatim_cooldown_until
    with _nominatim_state_lock:
        _nominatim_cooldown_until = max(
            _nominatim_cooldown_until,
            time.monotonic() + seconds,
        )


def _nominatim_cooldown_for_error(exc: Exception) -> int:
    if isinstance(exc, (httpx.RequestError, ValueError)):
        return 60
    if not isinstance(exc, httpx.HTTPStatusError):
        return 0
    status = exc.response.status_code
    if status == 403:
        return 15 * 60
    if status == 429 or status >= 500:
        return 60
    return 0


def _reverse_cache_get(key: tuple[float, float]):
    now = time.monotonic()
    with _reverse_cache_lock:
        entry = _reverse_cache.get(key)
        if entry is None:
            return _CACHE_MISS
        expires_at, result = entry
        if expires_at <= now:
            _reverse_cache.pop(key, None)
            return _CACHE_MISS
        return result


def _reverse_cache_put(key: tuple[float, float], result: GeocodingResult | None) -> None:
    ttl = _REVERSE_CACHE_TTL if result is not None else _REVERSE_NEGATIVE_CACHE_TTL
    with _reverse_cache_lock:
        _reverse_cache[key] = (time.monotonic() + ttl, result)
        while len(_reverse_cache) > _REVERSE_CACHE_MAX_ENTRIES:
            oldest = next(iter(_reverse_cache))
            _reverse_cache.pop(oldest, None)


class GeocodingService:
    """Async wrapper around forward / reverse geocoding."""

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": NOMINATIM_USER_AGENT,
            "Accept": "application/json",
        }

    # ------------------------------------------------------------------
    # Forward geocoding — dispatcher
    # ------------------------------------------------------------------

    async def search(
        self,
        query: str,
        limit: int = 5,
        provider: str = "nominatim",
        google_key: str | None = None,
    ) -> list[GeocodingResult]:
        """Forward geocode: address or place name -> coordinates."""
        if provider == "google":
            if not google_key:
                raise HTTPException(
                    status_code=400,
                    detail="provider=google requires google_key",
                )
            return await self._search_google(query, limit, google_key)
        if provider == "photon":
            return await self._search_photon(query, limit)
        return await self._search_nominatim(query, limit)

    async def _search_nominatim(self, query: str, limit: int) -> list[GeocodingResult]:
        params = {
            "q": query,
            "format": "json",
            "limit": min(limit, 40),
        }
        logger.debug("Nominatim search: %s", query)
        try:
            resp = await _nominatim_get(
                f"{NOMINATIM_BASE_URL}/search",
                params=params,
            )
            if resp is None:
                logger.info("Nominatim search cooling down; using Photon")
                return await self._search_photon(query, limit)
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, list):
                raise ValueError("Nominatim search response must be a JSON list")
        except (httpx.HTTPStatusError, httpx.RequestError, ValueError) as exc:
            cooldown = _nominatim_cooldown_for_error(exc)
            status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
            reason = f"HTTP {status}" if status else (
                "invalid response" if isinstance(exc, ValueError) else "network error"
            )
            if cooldown:
                _suspend_nominatim(cooldown)
            logger.warning(
                "Nominatim search unavailable (status=%s); falling back to Photon%s",
                reason,
                f" for {cooldown}s" if cooldown else "",
            )
            return await self._search_photon(query, limit)

        results: list[GeocodingResult] = []
        for item in data:
            try:
                results.append(
                    GeocodingResult(
                        display_name=item.get("display_name", ""),
                        lat=float(item["lat"]),
                        lng=float(item["lon"]),
                        type=item.get("type", ""),
                        importance=float(item.get("importance", 0)),
                    )
                )
            except (AttributeError, KeyError, TypeError, ValueError) as exc:
                logger.warning("Skipping malformed search result: %s", exc)
        return results

    async def _search_photon(self, query: str, limit: int) -> list[GeocodingResult]:
        # Photon returns GeoJSON: a FeatureCollection where each feature has
        # `geometry.coordinates = [lon, lat]` and `properties` with name /
        # city / country / etc. There's no `display_name` field, so we
        # synthesise one from the properties for parity with Nominatim.
        params = {"q": query, "limit": min(limit, 40)}
        logger.debug("Photon search: %s", query)
        resp = await _get_client().get(
            f"{PHOTON_BASE_URL}/api",
            params=params,
            headers={"User-Agent": NOMINATIM_USER_AGENT},
        )
        resp.raise_for_status()
        data = resp.json()

        results: list[GeocodingResult] = []
        for feat in data.get("features", []):
            try:
                coords = feat["geometry"]["coordinates"]
                lng, lat = float(coords[0]), float(coords[1])
                props = feat.get("properties") or {}
                name = (props.get("name") or "").strip()
                # Build a Nominatim-style "specific, generic, country" line.
                parts: list[str] = []
                if name:
                    parts.append(name)
                house = props.get("housenumber")
                street = props.get("street")
                if house and street:
                    parts.append(f"{street} {house}")
                elif street:
                    parts.append(street)
                for key in ("district", "city", "county", "state", "country"):
                    v = props.get(key)
                    if v and v not in parts:
                        parts.append(v)
                display = ", ".join(parts) if parts else (name or query)
                results.append(
                    GeocodingResult(
                        display_name=display,
                        lat=lat,
                        lng=lng,
                        # Photon's "type" is the OSM tag value (e.g. "city"),
                        # mirror it into our `type` field for compat.
                        type=props.get("type") or props.get("osm_value") or "",
                        importance=0.0,
                        country_code=(props.get("countrycode") or "").lower(),
                        short_name=name,
                    )
                )
            except (KeyError, ValueError, TypeError) as exc:
                logger.warning("Skipping malformed Photon result: %s", exc)
        return results

    async def _search_google(
        self, query: str, limit: int, api_key: str
    ) -> list[GeocodingResult]:
        # Google's Geocoding API doesn't take a `limit` — it returns a
        # capped list (usually 1, sometimes more for ambiguous queries).
        # We slice client-side after the fact so behaviour matches the
        # Nominatim path.
        params = {
            "address": query,
            "key": api_key,
            "language": "zh-TW",
        }
        logger.debug("Google geocode search: %s", query)
        resp = await _get_client().get(_GOOGLE_GEOCODE_URL, params=params)
        if resp.status_code != 200:
            text = resp.text[:200] if resp.text else ""
            raise HTTPException(
                status_code=502,
                detail=f"Google geocode HTTP {resp.status_code}: {text}",
            )
        data = resp.json()
        status = data.get("status")
        if status not in ("OK", "ZERO_RESULTS"):
            # Surface Google's own error text so the user can fix things
            # like REQUEST_DENIED (key invalid / API not enabled) and
            # OVER_QUERY_LIMIT (free tier exhausted).
            err_msg = data.get("error_message") or status or "unknown error"
            raise HTTPException(
                status_code=502,
                detail=f"Google geocode {status}: {err_msg}",
            )

        results: list[GeocodingResult] = []
        for item in (data.get("results") or [])[:limit]:
            try:
                loc = item["geometry"]["location"]
                # Google's `types` is a list like ["street_address"];
                # take the first as our `type` field for compat with the
                # Nominatim shape.
                types = item.get("types") or []
                results.append(
                    GeocodingResult(
                        display_name=item.get("formatted_address", ""),
                        lat=float(loc["lat"]),
                        lng=float(loc["lng"]),
                        type=types[0] if types else "",
                        importance=0.0,  # Google doesn't expose this
                    )
                )
            except (KeyError, ValueError, TypeError) as exc:
                logger.warning("Skipping malformed Google result: %s", exc)
        return results

    # ------------------------------------------------------------------
    # Reverse geocoding
    # ------------------------------------------------------------------

    async def reverse(self, lat: float, lng: float) -> GeocodingResult | None:
        """Reverse geocode: coordinates -> address.

        Returns ``None`` when no result is found.
        """
        cache_key = (round(lat, 6), round(lng, 6))
        cached = _reverse_cache_get(cache_key)
        if cached is not _CACHE_MISS:
            return cached

        # Coalesce concurrent lookups for the same coordinates without
        # serializing Photon fallbacks for unrelated locations.
        with _reverse_inflight_lock:
            entry = _reverse_inflight.get(cache_key)
            if entry is None:
                lookup_lock = asyncio.Lock()
                waiter_count = 1
            else:
                lookup_lock, waiter_count = entry
                waiter_count += 1
            _reverse_inflight[cache_key] = (lookup_lock, waiter_count)

        try:
            async with lookup_lock:
                cached = _reverse_cache_get(cache_key)
                if cached is not _CACHE_MISS:
                    return cached
                result = await self._reverse_uncached(lat, lng)
                _reverse_cache_put(cache_key, result)
                return result
        finally:
            with _reverse_inflight_lock:
                entry = _reverse_inflight.get(cache_key)
                if entry is not None and entry[0] is lookup_lock:
                    if entry[1] == 1:
                        _reverse_inflight.pop(cache_key)
                    else:
                        _reverse_inflight[cache_key] = (lookup_lock, entry[1] - 1)

    async def _reverse_uncached(self, lat: float, lng: float) -> GeocodingResult | None:
        params = {
            "lat": lat,
            "lon": lng,
            "format": "json",
            "addressdetails": 1,  # needed so response includes address.country_code
        }

        logger.debug("Nominatim reverse: %.6f, %.6f", lat, lng)

        try:
            resp = await _nominatim_get(
                f"{NOMINATIM_BASE_URL}/reverse",
                params=params,
            )
            if resp is None:
                return await self._reverse_photon(lat, lng)
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict):
                raise ValueError("Nominatim reverse response must be a JSON object")
        except (httpx.HTTPStatusError, httpx.RequestError, ValueError) as exc:
            status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
            cooldown = _nominatim_cooldown_for_error(exc)
            reason = f"HTTP {status}" if status else (
                "invalid response" if isinstance(exc, ValueError) else "network error"
            )
            if cooldown:
                _suspend_nominatim(cooldown)
            logger.warning(
                "Nominatim reverse unavailable (status=%s); falling back to Photon%s",
                reason,
                f" for {cooldown}s" if cooldown else "",
            )
            return await self._reverse_photon(lat, lng)

        if "error" in data:
            logger.info("Nominatim reverse returned error: %s", data["error"])
            return None

        try:
            addr = data.get("address") or {}
            display_name = data.get("display_name", "")
            short = _pick_short_name(addr, data.get("name") or "", display_name)
            result = GeocodingResult(
                display_name=display_name,
                lat=float(data["lat"]),
                lng=float(data["lon"]),
                type=data.get("type", ""),
                importance=float(data.get("importance", 0)),
                country_code=(addr.get("country_code") or "").lower(),
                short_name=short,
            )
            return result
        except (AttributeError, KeyError, ValueError, TypeError) as exc:
            logger.warning("Failed to parse Nominatim reverse result; trying Photon: %s", exc)
            return await self._reverse_photon(lat, lng)

    async def _reverse_photon(self, lat: float, lng: float) -> GeocodingResult | None:
        """Use Photon when Nominatim is unavailable or has rate-limited us."""
        try:
            resp = await _get_client().get(
                f"{PHOTON_BASE_URL}/reverse",
                params={"lat": lat, "lon": lng},
                headers=self._headers(),
            )
            resp.raise_for_status()
            features = resp.json().get("features") or []
            for feature in features:
                geometry = feature.get("geometry") or {}
                coordinates = geometry.get("coordinates") or []
                if len(coordinates) < 2:
                    continue
                props = feature.get("properties") or {}
                name = str(props.get("name") or "").strip()
                house = props.get("housenumber")
                street = props.get("street")
                parts = [name] if name else []
                if house and street:
                    parts.append(f"{street} {house}")
                elif street:
                    parts.append(str(street))
                for key in ("district", "city", "county", "state", "country"):
                    value = str(props.get(key) or "").strip()
                    if value and value not in parts:
                        parts.append(value)
                short_name = name or next(
                    (str(props[key]).strip() for key in ("district", "city", "county", "state", "country") if props.get(key)),
                    "",
                )
                return GeocodingResult(
                    display_name=", ".join(parts) or short_name,
                    lat=float(coordinates[1]),
                    lng=float(coordinates[0]),
                    type=props.get("type") or props.get("osm_value") or "",
                    importance=0.0,
                    country_code=(props.get("countrycode") or "").lower(),
                    short_name=short_name,
                )
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            logger.warning("Photon reverse fallback failed: %s", exc)
        return None


def _pick_short_name(addr: dict, name: str, display_name: str) -> str:
    """Pick a human-friendly short label from Nominatim's address details.

    Nominatim's display_name leads with the most granular component first
    (e.g. house number, then road, then suburb), which means naively taking
    the first comma-separated segment gives noise like '6' or '6號'. Prefer
    named POIs / features when present, then the street, then a region.
    """
    # Nominatim sometimes sets `name` at the top level for POIs.
    if name and len(name) > 1:
        return name.strip()
    # Address-level POI tags (ordered by how specific they are).
    poi_keys = (
        "tourism", "attraction", "building",
        "amenity", "shop", "leisure", "office",
        "historic", "public_transport", "railway",
    )
    for k in poi_keys:
        v = addr.get(k)
        if v and isinstance(v, str) and len(v) > 1:
            return v.strip()
    # Fall through to street / area names.
    for k in ("road", "pedestrian", "footway", "path"):
        v = addr.get(k)
        if v:
            return v.strip()
    for k in ("neighbourhood", "hamlet", "village", "suburb", "quarter"):
        v = addr.get(k)
        if v:
            return v.strip()
    for k in ("city_district", "town", "city", "municipality", "county"):
        v = addr.get(k)
        if v:
            return v.strip()
    # As a last resort, return the first comma segment that looks like a name
    # (length > 2 and not purely digits / house-number-ish).
    for seg in (s.strip() for s in display_name.split(",")):
        if len(seg) > 2 and not seg.replace("號", "").strip().isdigit():
            return seg
    return display_name.split(",")[0].strip() if display_name else ""

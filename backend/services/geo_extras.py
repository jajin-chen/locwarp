"""Extra geo services wired up in v0.2.22+.

- Timezone (TimezoneDB)
- Photon search / reverse (komoot's OSM geocoder, fallback to Nominatim)
- Overpass 'nearby POI' lookup
- OSRM table-based multi-stop waypoint optimization
"""

from __future__ import annotations

import logging
import math
from itertools import permutations

import httpx

from config import OSRM_BASE_URL
from models.schemas import (
    Coordinate,
    GeocodingResult,
    NearbyPoi,
    TimezoneInfo,
)

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(10.0, connect=5.0)

# ── TimezoneDB ────────────────────────────────────────────

TIMEZONEDB_KEY = "7JDL6A118RWJ"
TIMEZONEDB_URL = "https://api.timezonedb.com/v2.1/get-time-zone"


async def get_timezone(lat: float, lng: float) -> TimezoneInfo | None:
    params = {"key": TIMEZONEDB_KEY, "format": "json", "by": "position", "lat": lat, "lng": lng}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(TIMEZONEDB_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
        if data.get("status") != "OK":
            logger.info("TimezoneDB returned non-OK: %s", data.get("message"))
            return None
        return TimezoneInfo(
            zone=data.get("zoneName", ""),
            gmt_offset_seconds=int(data.get("gmtOffset", 0)),
            abbreviation=data.get("abbreviation", ""),
            timestamp=int(data.get("timestamp", 0)),
        )
    except httpx.HTTPError as e:
        logger.warning("TimezoneDB request failed: %s", e)
        return None


# ── Photon (komoot) with Nominatim fallback ────────────────

PHOTON_BASE_URL = "https://photon.komoot.io"
NOMINATIM_FALLBACK_URL = "https://nominatim.openstreetmap.org"


def _photon_to_result(feat: dict) -> GeocodingResult | None:
    try:
        geom = feat.get("geometry") or {}
        coords = geom.get("coordinates") or []
        if len(coords) < 2:
            return None
        lng, lat = float(coords[0]), float(coords[1])
        props = feat.get("properties") or {}
        # Build a display_name similar to Nominatim's comma-separated path.
        parts = []
        for key in ("name", "street", "housenumber", "suburb", "city",
                    "county", "state", "postcode", "country"):
            val = props.get(key)
            if val and val not in parts:
                parts.append(str(val))
        display_name = ", ".join(parts) if parts else props.get("name", "")
        return GeocodingResult(
            display_name=display_name,
            lat=lat,
            lng=lng,
            type=props.get("osm_key", "") or props.get("type", ""),
            importance=0.0,
            country_code=(props.get("countrycode") or "").lower(),
        )
    except (KeyError, ValueError, TypeError):
        return None


async def photon_search(query: str, limit: int = 5) -> list[GeocodingResult]:
    params = {"q": query, "limit": min(limit, 40)}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(f"{PHOTON_BASE_URL}/api", params=params)
            resp.raise_for_status()
            data = resp.json()
        feats = data.get("features") or []
        out: list[GeocodingResult] = []
        for f in feats:
            r = _photon_to_result(f)
            if r:
                out.append(r)
        return out
    except httpx.HTTPError as e:
        logger.info("Photon search failed, will fall back: %s", e)
        return []


async def photon_reverse(lat: float, lng: float) -> GeocodingResult | None:
    params = {"lat": lat, "lon": lng}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(f"{PHOTON_BASE_URL}/reverse", params=params)
            resp.raise_for_status()
            data = resp.json()
        feats = data.get("features") or []
        for f in feats:
            r = _photon_to_result(f)
            if r:
                return r
        return None
    except httpx.HTTPError as e:
        logger.info("Photon reverse failed, will fall back: %s", e)
        return None


# ── Overpass 'nearby POI' ──────────────────────────────────

OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# Categories worth showing to the user (amenity, shop, tourism, public_transport).
# Each maps to a tag filter used inside the Overpass query.
POI_CATEGORIES = {
    "amenity": "restaurant|cafe|fast_food|bar|pub|bank|atm|pharmacy|hospital|clinic|police|fuel|parking|toilets|school|library",
    "shop": "convenience|supermarket|mall|department_store",
    "tourism": "attraction|museum|viewpoint|hotel|hostel|guest_house",
    "public_transport": "station|stop_position|platform",
    "railway": "station|halt|subway_entrance",
    "highway": "bus_stop",
}


def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 6371000.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


async def nearby_pois(lat: float, lng: float, radius_m: int = 200, limit: int = 40) -> list[NearbyPoi]:
    filters = []
    for tag, values in POI_CATEGORIES.items():
        filters.append(f'node(around:{radius_m},{lat},{lng})["{tag}"~"^({values})$"];')
    query = "[out:json][timeout:15];\n(\n" + "\n".join(filters) + "\n);\nout body center;"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=5.0)) as client:
            resp = await client.post(OVERPASS_URL, data={"data": query})
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as e:
        logger.warning("Overpass query failed: %s", e)
        return []

    results: list[NearbyPoi] = []
    for el in data.get("elements") or []:
        try:
            plat = el.get("lat")
            plng = el.get("lon")
            if plat is None or plng is None:
                center = el.get("center") or {}
                plat = center.get("lat")
                plng = center.get("lon")
            if plat is None or plng is None:
                continue
            tags = el.get("tags") or {}
            name = tags.get("name") or tags.get("name:en") or tags.get("ref") or ""
            if not name:
                continue  # skip unnamed POIs to keep list focused
            cat, sub = "", ""
            for k in ("amenity", "shop", "tourism", "public_transport", "railway", "highway"):
                if k in tags:
                    cat = k
                    sub = tags[k]
                    break
            results.append(NearbyPoi(
                id=str(el.get("id") or f"{plat},{plng}"),
                name=name,
                category=cat,
                subcategory=sub,
                lat=float(plat),
                lng=float(plng),
                distance_m=_haversine_m(lat, lng, float(plat), float(plng)),
            ))
        except (ValueError, TypeError):
            continue

    results.sort(key=lambda p: p.distance_m)
    return results[:limit]


# ── OSRM Table multi-stop optimize ─────────────────────────

async def osrm_table(coords: list[Coordinate], profile: str = "foot") -> list[list[float]] | None:
    """Return duration matrix in seconds from OSRM Table API."""
    osrm_profile = "foot" if profile in ("foot", "walking", "running") else "car"
    coord_str = ";".join(f"{c.lng},{c.lat}" for c in coords)
    url = f"{OSRM_BASE_URL}/table/v1/{osrm_profile}/{coord_str}?annotations=duration"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
        if data.get("code") != "Ok":
            return None
        return data.get("durations")
    except httpx.HTTPError as e:
        logger.warning("OSRM table failed: %s", e)
        return None


def optimize_order_nearest_neighbor(
    durations: list[list[float]], keep_first: bool,
) -> list[int]:
    """Nearest-neighbour TSP heuristic. For small N (<=10) this is close
    to optimal and avoids pulling in a full TSP solver."""
    n = len(durations)
    if n <= 2:
        return list(range(n))
    start = 0 if keep_first else min(range(n), key=lambda i: sum(durations[i]))
    visited = {start}
    order = [start]
    while len(visited) < n:
        cur = order[-1]
        best = None
        best_d = float("inf")
        for j in range(n):
            if j in visited:
                continue
            d = durations[cur][j]
            if d is None:
                continue
            if d < best_d:
                best_d = d
                best = j
        if best is None:
            break
        order.append(best)
        visited.add(best)
    return order


def _route_total(durations: list[list[float]], order: list[int]) -> float:
    total = 0.0
    for a, b in zip(order, order[1:]):
        d = durations[a][b]
        if d is None:
            return float("inf")
        total += d
    return total


def optimize_order_exact(
    durations: list[list[float]], keep_first: bool,
) -> list[int]:
    """Brute-force optimal ordering for <=8 points."""
    n = len(durations)
    indices = list(range(n))
    if keep_first:
        head = [0]
        rest = indices[1:]
    else:
        head = []
        rest = indices
    best_order: list[int] = indices
    best_d = float("inf")
    for perm in permutations(rest):
        order = head + list(perm)
        d = _route_total(durations, order)
        if d < best_d:
            best_d = d
            best_order = order
    return best_order

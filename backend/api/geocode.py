import logging

import httpx
from fastapi import APIRouter, HTTPException

from models.schemas import (
    Coordinate,
    GeocodingResult,
    RouteOptimizeRequest,
    RouteOptimizeResponse,
    TimezoneInfo,
)
from services.geocoding import GeocodingService
from services.geo_extras import (
    get_timezone,
    haversine_duration_matrix,
    optimize_order_exact,
    optimize_order_nearest_neighbor,
    osrm_table,
)

router = APIRouter(prefix="/api/geocode", tags=["geocode"])
logger = logging.getLogger("locwarp")

geocoding_service = GeocodingService()


@router.get("/search", response_model=list[GeocodingResult])
async def search_address(q: str, limit: int = 5):
    return await geocoding_service.search(q, limit)


@router.get("/reverse", response_model=GeocodingResult | None)
async def reverse_geocode(lat: float, lng: float):
    return await geocoding_service.reverse(lat, lng)


@router.get("/timezone", response_model=TimezoneInfo | None)
async def timezone_lookup(lat: float, lng: float):
    """Return IANA timezone + UTC offset for a coordinate (TimezoneDB)."""
    return await get_timezone(lat, lng)


@router.get("/real-location")
async def real_location():
    """Resolve the user's real public IP to city-level coordinates.

    Runs on the backend (not the Electron renderer) so we bypass CORS and
    TLS-cert issues that killed the renderer-direct version. Tries three
    free providers in sequence and returns the first one that gives us a
    valid lat/lng.

    Returns: {"lat": float, "lng": float, "city": str, "country": str}
    Raises 502 if every provider fails.
    """
    providers = [
        # (name, url, extractor)
        (
            "ipwho.is",
            "https://ipwho.is/?fields=success,latitude,longitude,city,region,country",
            lambda d: None
            if d.get("success") is False
            else (
                float(d["latitude"]),
                float(d["longitude"]),
                str(d.get("city") or d.get("region") or ""),
                str(d.get("country") or ""),
            )
            if d.get("latitude") is not None and d.get("longitude") is not None
            else None,
        ),
        (
            "ip-api.com",
            "http://ip-api.com/json/?fields=status,lat,lon,city,regionName,country",
            lambda d: (
                float(d["lat"]),
                float(d["lon"]),
                str(d.get("city") or d.get("regionName") or ""),
                str(d.get("country") or ""),
            )
            if d.get("status") == "success"
            else None,
        ),
        (
            "ipapi.co",
            "https://ipapi.co/json/",
            lambda d: (
                float(d["latitude"]),
                float(d["longitude"]),
                str(d.get("city") or d.get("region") or ""),
                str(d.get("country_name") or d.get("country") or ""),
            )
            if d.get("latitude") is not None and d.get("longitude") is not None
            else None,
        ),
    ]

    last_err: str = ""
    async with httpx.AsyncClient(timeout=httpx.Timeout(6.0, connect=3.0)) as client:
        for name, url, extract in providers:
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()
                result = extract(data)
                if result is None:
                    last_err = f"{name} returned no location"
                    continue
                lat, lng, city, country = result
                logger.info("real-location resolved via %s: %.4f, %.4f (%s)", name, lat, lng, city)
                return {"lat": lat, "lng": lng, "city": city, "country": country}
            except Exception as exc:
                last_err = f"{name}: {exc}"
                logger.info("real-location provider %s failed: %s", name, exc)
                continue

    raise HTTPException(status_code=502, detail=f"All IP geolocation providers failed ({last_err})")


@router.post("/route-optimize", response_model=RouteOptimizeResponse)
async def route_optimize(req: RouteOptimizeRequest):
    """Reorder waypoints to minimize total travel time.

    Uses OSRM /table when feasible (<=100 waypoints AND the demo server
    responds). Otherwise falls back to a straight-line haversine duration
    matrix — accuracy trades road distance for crow-flight, which is fine
    for dense Pokemon-GO style loops where adjacent points are already
    close together. The endpoint always succeeds; no more 503.
    """
    if len(req.waypoints) < 2:
        raise HTTPException(status_code=400, detail="need >=2 waypoints")
    durations = await osrm_table(req.waypoints, req.profile)
    used_estimate = False
    if not durations:
        durations = haversine_duration_matrix(req.waypoints, req.profile)
        used_estimate = True
        logger.info(
            "route_optimize: using haversine fallback for %d waypoints", len(req.waypoints),
        )

    # Brute-force optimal up to 8 points, heuristic beyond. With the
    # haversine matrix the brute-force is still cheap (8! = 40320 perms).
    if len(req.waypoints) <= 8:
        order = optimize_order_exact(durations, req.keep_first)
    else:
        order = optimize_order_nearest_neighbor(durations, req.keep_first)

    reordered = [req.waypoints[i] for i in order]
    total_duration = 0.0
    for a, b in zip(order, order[1:]):
        d = durations[a][b] or 0.0
        total_duration += d

    return RouteOptimizeResponse(
        waypoints=[Coordinate(lat=wp.lat, lng=wp.lng) for wp in reordered],
        total_distance_m=0.0,
        total_duration_s=total_duration,
        used_estimate=used_estimate,
    )

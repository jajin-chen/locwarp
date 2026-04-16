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
    optimize_order_exact,
    optimize_order_nearest_neighbor,
    osrm_table,
)

router = APIRouter(prefix="/api/geocode", tags=["geocode"])

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


@router.post("/route-optimize", response_model=RouteOptimizeResponse)
async def route_optimize(req: RouteOptimizeRequest):
    """Reorder waypoints to minimize total travel time using OSRM Table."""
    if len(req.waypoints) < 2:
        raise HTTPException(status_code=400, detail="need >=2 waypoints")
    durations = await osrm_table(req.waypoints, req.profile)
    if not durations:
        raise HTTPException(status_code=503, detail="OSRM Table unavailable")

    # Brute-force optimal up to 8 points, heuristic beyond.
    if len(req.waypoints) <= 8:
        order = optimize_order_exact(durations, req.keep_first)
    else:
        order = optimize_order_nearest_neighbor(durations, req.keep_first)

    reordered = [req.waypoints[i] for i in order]
    total_duration = 0.0
    for a, b in zip(order, order[1:]):
        d = durations[a][b] or 0.0
        total_duration += d

    # Distance not directly returned by Table without ?annotations=distance.
    # Return 0 for distance; frontend can recompute if needed.
    return RouteOptimizeResponse(
        waypoints=[Coordinate(lat=wp.lat, lng=wp.lng) for wp in reordered],
        total_distance_m=0.0,
        total_duration_s=total_duration,
    )

"""OSRM route planning service."""

from __future__ import annotations

import logging

import httpx

from config import OSRM_BASE_URL

logger = logging.getLogger(__name__)

# Map user-facing profile names to OSRM profile slugs
_PROFILE_MAP = {
    "walking": "foot",
    "running": "foot",
    "driving": "car",
    "foot": "foot",
    "car": "car",
    "bike": "bike",
    "bicycle": "bicycle",
}

_TIMEOUT = httpx.Timeout(8.0, connect=4.0)


def _haversine_m(a_lat: float, a_lng: float, b_lat: float, b_lng: float) -> float:
    """Great-circle distance in meters."""
    import math
    R = 6371000.0
    dlat = math.radians(b_lat - a_lat)
    dlng = math.radians(b_lng - a_lng)
    la1 = math.radians(a_lat)
    la2 = math.radians(b_lat)
    h = math.sin(dlat / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin(dlng / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def _straight_line_fallback(waypoints: list[tuple[float, float]], walking_speed_mps: float = 1.4) -> dict:
    """Construct a straight-line route as a last resort when OSRM is unreachable.
    Densifies each segment so the interpolator has enough sample points."""
    coords: list[list[float]] = [[waypoints[0][0], waypoints[0][1]]]
    total_distance = 0.0
    leg_durations: list[float] = []
    step_m = 25.0
    for i in range(len(waypoints) - 1):
        a_lat, a_lng = waypoints[i]
        b_lat, b_lng = waypoints[i + 1]
        seg_d = _haversine_m(a_lat, a_lng, b_lat, b_lng)
        steps = max(1, int(seg_d / step_m))
        for s in range(1, steps + 1):
            t = s / steps
            coords.append([a_lat + (b_lat - a_lat) * t, a_lng + (b_lng - a_lng) * t])
        total_distance += seg_d
        leg_durations.append(seg_d / walking_speed_mps)
    return {
        "coords": coords,
        "duration": total_distance / walking_speed_mps,
        "distance": total_distance,
        "leg_durations": leg_durations,
        "fallback": True,
    }


class RouteService:
    """Thin async wrapper around the OSRM HTTP API."""

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    async def get_route(
        self,
        start_lat: float,
        start_lng: float,
        end_lat: float,
        end_lng: float,
        profile: str = "foot",
        force_straight: bool = False,
    ) -> dict:
        """Plan a route between two points via OSRM.

        When *force_straight* is True, skip OSRM entirely and serve a
        densified straight-line route (used by the global "straight-line"
        toggle for users who want raw bearing-to-point travel).
        """
        waypoints = [
            (start_lat, start_lng),
            (end_lat, end_lng),
        ]
        if force_straight:
            return _straight_line_fallback(waypoints)
        return await self._fetch_route(waypoints, profile)

    async def get_multi_route(
        self,
        waypoints: list[tuple[float, float] | list[float] | dict],
        profile: str = "foot",
        force_straight: bool = False,
    ) -> dict:
        """Plan a route through multiple waypoints.

        *waypoints* may be a list of ``(lat, lng)`` tuples, ``[lat, lng]``
        lists, or dicts with ``lat``/``lng`` keys.
        """
        normalised: list[tuple[float, float]] = []
        for wp in waypoints:
            if isinstance(wp, dict):
                normalised.append((wp["lat"], wp["lng"]))
            else:
                normalised.append((float(wp[0]), float(wp[1])))

        if len(normalised) < 2:
            raise ValueError("At least two waypoints are required")

        if force_straight:
            return _straight_line_fallback(normalised)
        return await self._fetch_route(normalised, profile)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _fetch_route(
        self,
        waypoints: list[tuple[float, float]],
        profile: str,
    ) -> dict:
        osrm_profile = _PROFILE_MAP.get(profile, profile)

        # OSRM coordinate pairs are lon,lat (not lat,lon)
        coords_str = ";".join(
            f"{lng},{lat}" for lat, lng in waypoints
        )

        url = (
            f"{OSRM_BASE_URL}/route/v1/{osrm_profile}/{coords_str}"
            "?overview=full&geometries=geojson&steps=true"
            "&annotations=duration,distance"
        )

        logger.debug("OSRM request: %s", url)

        # Per-request fallback only; do NOT cache failures across a
        # region. A single transient OSRM blip (demo-server 502s, etc.)
        # would otherwise force every subsequent leg of a random walk
        # onto a straight line for the rest of the cache window.
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()
            if data.get("code") != "Ok":
                msg = data.get("message", "Unknown OSRM error")
                raise RuntimeError(f"OSRM error: {msg}")
        except (httpx.HTTPError, httpx.TimeoutException, RuntimeError) as e:
            logger.warning(
                "OSRM failed (%s); using straight-line fallback for this leg",
                type(e).__name__,
            )
            return _straight_line_fallback(waypoints)

        route = data["routes"][0]
        geometry = route["geometry"]  # GeoJSON LineString

        # GeoJSON coordinates are [lon, lat]; convert to [lat, lng]
        coords = [
            [pt[1], pt[0]] for pt in geometry["coordinates"]
        ]

        leg_durations = [leg["duration"] for leg in route["legs"]]

        return {
            "coords": coords,
            "duration": route["duration"],
            "distance": route["distance"],
            "leg_durations": leg_durations,
        }

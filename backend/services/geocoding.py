"""Nominatim forward / reverse geocoding service."""

from __future__ import annotations

import logging

import httpx

from config import NOMINATIM_BASE_URL, NOMINATIM_USER_AGENT
from models.schemas import GeocodingResult

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


class GeocodingService:
    """Async wrapper around the Nominatim geocoding API."""

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": NOMINATIM_USER_AGENT,
            "Accept": "application/json",
        }

    # ------------------------------------------------------------------
    # Forward geocoding
    # ------------------------------------------------------------------

    async def search(self, query: str, limit: int = 5) -> list[GeocodingResult]:
        """Forward geocode: address or place name -> coordinates.

        Parameters
        ----------
        query:
            Free-form search string (e.g. ``"Taipei 101"``).
        limit:
            Maximum number of results (default 5, Nominatim max 40).

        Returns
        -------
        list[GeocodingResult]
        """
        params = {
            "q": query,
            "format": "json",
            "limit": min(limit, 40),
        }

        logger.debug("Nominatim search: %s", query)

        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{NOMINATIM_BASE_URL}/search",
                params=params,
                headers=self._headers(),
            )
            resp.raise_for_status()
            data = resp.json()

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
            except (KeyError, ValueError) as exc:
                logger.warning("Skipping malformed search result: %s", exc)

        return results

    # ------------------------------------------------------------------
    # Reverse geocoding
    # ------------------------------------------------------------------

    async def reverse(self, lat: float, lng: float) -> GeocodingResult | None:
        """Reverse geocode: coordinates -> address.

        Returns ``None`` when no result is found.
        """
        params = {
            "lat": lat,
            "lon": lng,
            "format": "json",
            "addressdetails": 1,  # needed so response includes address.country_code
        }

        logger.debug("Nominatim reverse: %.6f, %.6f", lat, lng)

        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{NOMINATIM_BASE_URL}/reverse",
                params=params,
                headers=self._headers(),
            )
            resp.raise_for_status()
            data = resp.json()

        if "error" in data:
            logger.info("Nominatim reverse returned error: %s", data["error"])
            return None

        try:
            addr = data.get("address") or {}
            display_name = data.get("display_name", "")
            short = _pick_short_name(addr, data.get("name") or "", display_name)
            return GeocodingResult(
                display_name=display_name,
                lat=float(data["lat"]),
                lng=float(data["lon"]),
                type=data.get("type", ""),
                importance=float(data.get("importance", 0)),
                country_code=(addr.get("country_code") or "").lower(),
                short_name=short,
            )
        except (KeyError, ValueError) as exc:
            logger.warning("Failed to parse reverse result: %s", exc)
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

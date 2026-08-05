"""Offline timezone lookup (tzfpy + zoneinfo) — replaces the TimezoneDB API.

get_timezone() must resolve entirely locally: no network, no API key. These
tests would fail against the old TimezoneDB implementation whenever the
shared key was rate-limited or the machine was offline; the offline
implementation must make them deterministic.
"""

from services.geo_extras import get_timezone


async def test_taipei_resolves_to_asia_taipei():
    tz = await get_timezone(25.033, 121.5654)
    assert tz is not None
    assert tz.zone == "Asia/Taipei"
    assert tz.gmt_offset_seconds == 28800  # UTC+8, no DST
    assert tz.abbreviation != ""


async def test_new_york_resolves_with_dst_aware_offset():
    tz = await get_timezone(40.7128, -74.006)
    assert tz is not None
    assert tz.zone == "America/New_York"
    # EST -18000 or EDT -14400 depending on date the test runs.
    assert tz.gmt_offset_seconds in (-18000, -14400)


async def test_open_ocean_falls_back_to_etc_gmt_zone():
    # tzf's data covers international waters with Etc/GMT±N zones, which the
    # old TimezoneDB API also returned. The status bar only needs zone+offset.
    tz = await get_timezone(0.0, -140.0)
    assert tz is not None
    assert tz.zone.startswith("Etc/GMT")


async def test_timestamp_is_local_wall_time():
    # Schema contract (models/schemas.py TimezoneInfo.timestamp): unix
    # timestamp shifted to the zone's current wall time, matching what
    # TimezoneDB used to return.
    import time

    tz = await get_timezone(25.033, 121.5654)
    assert tz is not None
    assert abs(tz.timestamp - (time.time() + tz.gmt_offset_seconds)) < 60

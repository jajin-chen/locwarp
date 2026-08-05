"""Shared test doubles for backend tests."""

from __future__ import annotations


class FakeLocationService:
    """In-memory stand-in for Dvt/Legacy LocationService.

    Records every pushed coordinate so tests can assert on the exact
    sequence the engine sent to the "device".
    """

    def __init__(self) -> None:
        self.positions: list[tuple[float, float]] = []
        self.clear_count: int = 0

    async def set(self, lat: float, lng: float) -> None:
        self.positions.append((lat, lng))

    async def clear(self) -> None:
        self.clear_count += 1

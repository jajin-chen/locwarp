"""Saved-route + category management with JSON file persistence.

Mirrors :mod:`services.bookmarks` so saved routes get the same ergonomics
as bookmarks (categories, search-friendly listing, recolor, rename).

Backward compatibility: legacy ``routes.json`` files have shape
``{"routes": [...]}`` without categories and without ``category_id``
on each route. Both gaps are handled by pydantic defaults: the bare
list still parses, and a single ``"default"`` category is injected on
load so the front-end always has at least one bucket to render.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from config import ROUTES_FILE
from models.schemas import RouteCategory, RouteStore, SavedRoute
from services.json_safe import safe_load_json, safe_write_json

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_category() -> RouteCategory:
    return RouteCategory(
        id="default",
        name="預設",
        color="#6c8cff",
        sort_order=0,
        created_at=_now_iso(),
    )


class RouteManager:
    """CRUD manager for saved routes and route categories.

    State is persisted to :data:`ROUTES_FILE` on every write.
    """

    def __init__(self) -> None:
        self.store = RouteStore(categories=[_default_category()], routes=[])
        self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        data = safe_load_json(Path(ROUTES_FILE))
        if data is None:
            logger.info("No routes file (or unreadable); using defaults")
            return

        # Two file shapes are accepted:
        #   - new:  {"routes": [...], "categories": [...]}
        #   - old:  {"routes": [...]}
        # In both cases pydantic fills missing fields with defaults.
        try:
            store = RouteStore(**data)
        except Exception as exc:
            logger.warning("Route payload failed schema validation: %s", exc)
            return

        # Guarantee a default category exists. Old files won't have one,
        # and even new files might if a user manually deleted everything.
        if not any(c.id == "default" for c in store.categories):
            store.categories.insert(0, _default_category())

        # Any route whose category_id points at a deleted category falls
        # back to default. Keeps the UI from rendering ghost groups.
        valid_ids = {c.id for c in store.categories}
        for r in store.routes:
            if r.category_id not in valid_ids:
                r.category_id = "default"

        self.store = store
        logger.info(
            "Loaded %d routes in %d categories",
            len(self.store.routes),
            len(self.store.categories),
        )

    def _save(self) -> None:
        payload = json.loads(self.store.model_dump_json())
        safe_write_json(Path(ROUTES_FILE), payload)

    # ------------------------------------------------------------------
    # Categories
    # ------------------------------------------------------------------

    def list_categories(self) -> list[RouteCategory]:
        return sorted(self.store.categories, key=lambda c: c.sort_order)

    def create_category(self, name: str, color: str = "#6c8cff") -> RouteCategory:
        max_order = max((c.sort_order for c in self.store.categories), default=-1)
        cat = RouteCategory(
            id=str(uuid.uuid4()),
            name=name,
            color=color,
            sort_order=max_order + 1,
            created_at=_now_iso(),
        )
        self.store.categories.append(cat)
        self._save()
        return cat

    def update_category(
        self,
        cat_id: str,
        name: str | None = None,
        color: str | None = None,
    ) -> RouteCategory | None:
        cat = self._find_category(cat_id)
        if cat is None:
            return None
        if name is not None:
            cat.name = name
        if color is not None:
            cat.color = color
        self._save()
        return cat

    def delete_category(self, cat_id: str) -> bool:
        if cat_id == "default":
            logger.warning("Cannot delete the default route category")
            return False
        cat = self._find_category(cat_id)
        if cat is None:
            return False
        for r in self.store.routes:
            if r.category_id == cat_id:
                r.category_id = "default"
        self.store.categories = [c for c in self.store.categories if c.id != cat_id]
        self._save()
        return True

    def _find_category(self, cat_id: str) -> RouteCategory | None:
        return next((c for c in self.store.categories if c.id == cat_id), None)

    # ------------------------------------------------------------------
    # Routes
    # ------------------------------------------------------------------

    def list_routes(self) -> list[SavedRoute]:
        return list(self.store.routes)

    def find_by_name(self, name: str) -> SavedRoute | None:
        """First exact-name match. Used by the overwrite-on-save flow on
        the rare cases the front-end doesn't already know the id."""
        target = name.strip()
        return next((r for r in self.store.routes if r.name == target), None)

    def create_route(self, route: SavedRoute) -> SavedRoute:
        # Validate category; fall back to default when the caller passed
        # something unknown (e.g. a category that's been deleted).
        if self._find_category(route.category_id) is None:
            route.category_id = "default"
        route.id = str(uuid.uuid4())
        route.created_at = _now_iso()
        route.updated_at = ""
        self.store.routes.append(route)
        self._save()
        return route

    def replace_route(self, route_id: str, incoming: SavedRoute) -> SavedRoute | None:
        """Replace an existing route's mutable fields in-place.

        Keeps id and created_at; updates name, waypoints, profile,
        category_id, and stamps updated_at. This is the backend half of
        the "save and overwrite same-named route" UX.
        """
        existing = self._find_route(route_id)
        if existing is None:
            return None
        if self._find_category(incoming.category_id) is None:
            incoming.category_id = existing.category_id
        existing.name = incoming.name
        existing.waypoints = incoming.waypoints
        existing.profile = incoming.profile
        existing.category_id = incoming.category_id
        existing.updated_at = _now_iso()
        self._save()
        return existing

    def rename_route(self, route_id: str, name: str) -> SavedRoute | None:
        route = self._find_route(route_id)
        if route is None:
            return None
        route.name = name
        route.updated_at = _now_iso()
        self._save()
        return route

    def delete_route(self, route_id: str) -> bool:
        before = len(self.store.routes)
        self.store.routes = [r for r in self.store.routes if r.id != route_id]
        if len(self.store.routes) < before:
            self._save()
            return True
        return False

    def move_routes(self, route_ids: list[str], target_category_id: str) -> int:
        if self._find_category(target_category_id) is None:
            logger.warning("Target route category %s does not exist", target_category_id)
            return 0
        ids_set = set(route_ids)
        moved = 0
        for r in self.store.routes:
            if r.id in ids_set and r.category_id != target_category_id:
                r.category_id = target_category_id
                r.updated_at = _now_iso()
                moved += 1
        if moved:
            self._save()
        return moved

    def _find_route(self, route_id: str) -> SavedRoute | None:
        return next((r for r in self.store.routes if r.id == route_id), None)

    # ------------------------------------------------------------------
    # Import / Export
    # ------------------------------------------------------------------

    def export_json(self) -> str:
        return self.store.model_dump_json(indent=2)

    def import_json(self, data: str) -> int:
        """Merge an exported route bundle into the current store.

        Behaviour:
          - Categories: imported only if their id is new.
          - Routes: imported with their original id when free; on id
            collision a fresh id is minted so the existing route is
            preserved. On name collision inside the same category the
            imported route gets a `(匯入)` suffix so both stay visible.
          - A route pointing at an unknown category_id falls back to default.
        """
        try:
            incoming = RouteStore(**json.loads(data))
        except Exception as exc:
            logger.error("Invalid route JSON: %s", exc)
            return 0

        existing_cat_ids = {c.id for c in self.store.categories}
        for cat in incoming.categories:
            if cat.id and cat.id not in existing_cat_ids:
                self.store.categories.append(cat)
                existing_cat_ids.add(cat.id)

        existing_route_ids = {r.id for r in self.store.routes}
        imported = 0
        for r in incoming.routes:
            if r.category_id not in existing_cat_ids:
                r.category_id = "default"
            if not r.id or r.id in existing_route_ids:
                r.id = str(uuid.uuid4())
            siblings = [
                s for s in self.store.routes
                if s.category_id == r.category_id and s.name == r.name
            ]
            if siblings:
                r.name = f"{r.name} (匯入)"
            if not r.created_at:
                r.created_at = _now_iso()
            self.store.routes.append(r)
            existing_route_ids.add(r.id)
            imported += 1

        if imported:
            self._save()
        logger.info("Imported %d routes", imported)
        return imported

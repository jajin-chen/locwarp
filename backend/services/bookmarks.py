"""Bookmark and category management with JSON file persistence."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from config import BOOKMARKS_FILE
from models.schemas import Bookmark, BookmarkCategory, BookmarkStore
from services.json_safe import safe_load_json, safe_write_json

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class BookmarkManager:
    """CRUD manager for bookmarks and categories.

    State is persisted to :data:`BOOKMARKS_FILE` (JSON) on every write
    operation.
    """

    def __init__(self) -> None:
        self.store = BookmarkStore(
            categories=[
                BookmarkCategory(
                    id="default",
                    name="預設",
                    color="#6c8cff",
                    sort_order=0,
                    created_at=_now_iso(),
                )
            ],
            bookmarks=[],
        )
        self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Load bookmarks from the JSON file, if it exists.

        Uses ``safe_load_json`` so a parse failure does not silently
        discard the user's data: the corrupt file is renamed aside as
        ``bookmarks.json.bak-<timestamp>`` before we fall back to the
        default empty store. Otherwise the next ``_save()`` would
        overwrite the original file with an empty bookmark list.
        """
        data = safe_load_json(Path(BOOKMARKS_FILE))
        if data is None:
            logger.info("No bookmark file (or unreadable); using defaults")
            return
        try:
            self.store = BookmarkStore(**data)
            logger.info(
                "Loaded %d bookmarks in %d categories",
                len(self.store.bookmarks),
                len(self.store.categories),
            )
        except Exception as exc:
            logger.warning("Bookmark payload failed schema validation: %s", exc)

    def _save(self) -> None:
        """Persist the current store to disk via atomic tmp + rename."""
        payload = json.loads(self.store.model_dump_json())
        safe_write_json(Path(BOOKMARKS_FILE), payload)

    # ------------------------------------------------------------------
    # Categories
    # ------------------------------------------------------------------

    def create_category(
        self,
        name: str,
        color: str = "#6c8cff",
    ) -> BookmarkCategory:
        """Create and return a new category."""
        max_order = max((c.sort_order for c in self.store.categories), default=-1)
        cat = BookmarkCategory(
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
    ) -> BookmarkCategory | None:
        """Update a category's name or colour. Returns ``None`` if not found."""
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
        """Delete a category and move its bookmarks to *default*.

        The *default* category cannot be deleted.
        """
        if cat_id == "default":
            logger.warning("Cannot delete the default category")
            return False

        cat = self._find_category(cat_id)
        if cat is None:
            return False

        # Move orphaned bookmarks
        for bm in self.store.bookmarks:
            if bm.category_id == cat_id:
                bm.category_id = "default"

        self.store.categories = [c for c in self.store.categories if c.id != cat_id]
        self._save()
        return True

    def list_categories(self) -> list[BookmarkCategory]:
        return sorted(self.store.categories, key=lambda c: c.sort_order)

    def reorder_categories(self, category_ids: list[str]) -> int:
        """Reassign ``sort_order`` so categories appear in the given order.

        Categories whose ids are not present in *category_ids* are pushed to
        the end while preserving their current relative order, so callers may
        send only the subset they've actually rearranged.
        """
        order_map = {cid: idx for idx, cid in enumerate(category_ids)}
        seen = set(category_ids)
        # Append unspecified ones in their current sort_order order.
        tail_idx = len(category_ids)
        existing_sorted = sorted(self.store.categories, key=lambda c: c.sort_order)
        for cat in existing_sorted:
            if cat.id not in seen:
                order_map[cat.id] = tail_idx
                tail_idx += 1
        changed = 0
        for cat in self.store.categories:
            new_order = order_map.get(cat.id, cat.sort_order)
            if cat.sort_order != new_order:
                cat.sort_order = new_order
                changed += 1
        if changed:
            self._save()
        return changed

    def reorder_bookmarks_in_category(
        self,
        category_id: str,
        bookmark_ids: list[str],
    ) -> int:
        """Reorder bookmarks belonging to *category_id* to match *bookmark_ids*.

        Items in the category but missing from *bookmark_ids* are appended at
        the end of the rearranged block (preserving their current order).
        Items outside the category keep their original positions in the store
        list, so other categories' ordering is unaffected.
        """
        in_cat_indices = [
            i for i, b in enumerate(self.store.bookmarks)
            if b.category_id == category_id
        ]
        if not in_cat_indices:
            return 0
        in_cat_bms = [self.store.bookmarks[i] for i in in_cat_indices]
        bm_by_id = {b.id: b for b in in_cat_bms}
        ordered: list[Bookmark] = []
        consumed: set[str] = set()
        for bid in bookmark_ids:
            bm = bm_by_id.get(bid)
            if bm is not None and bid not in consumed:
                ordered.append(bm)
                consumed.add(bid)
        for bm in in_cat_bms:
            if bm.id not in consumed:
                ordered.append(bm)
        new_list = list(self.store.bookmarks)
        for slot, bm in zip(in_cat_indices, ordered):
            new_list[slot] = bm
        if new_list != self.store.bookmarks:
            self.store.bookmarks = new_list
            self._save()
        return len(ordered)

    def _find_category(self, cat_id: str) -> BookmarkCategory | None:
        return next((c for c in self.store.categories if c.id == cat_id), None)

    # ------------------------------------------------------------------
    # Bookmarks
    # ------------------------------------------------------------------

    def create_bookmark(
        self,
        name: str,
        lat: float,
        lng: float,
        address: str = "",
        category_id: str = "default",
        country_code: str = "",
    ) -> Bookmark:
        """Create a new bookmark."""
        # Validate category
        if self._find_category(category_id) is None:
            category_id = "default"

        now = _now_iso()
        bm = Bookmark(
            id=str(uuid.uuid4()),
            name=name,
            lat=lat,
            lng=lng,
            address=address,
            category_id=category_id,
            created_at=now,
            last_used_at=now,
            country_code=country_code.lower(),
        )
        self.store.bookmarks.append(bm)
        self._save()
        return bm

    def update_bookmark(self, bm_id: str, **kwargs: object) -> Bookmark | None:
        """Update a bookmark's fields. Returns ``None`` if not found."""
        bm = self._find_bookmark(bm_id)
        if bm is None:
            return None

        allowed = {"name", "lat", "lng", "address", "category_id", "last_used_at", "country_code"}
        for key, value in kwargs.items():
            if key in allowed and value is not None:
                setattr(bm, key, value)

        self._save()
        return bm

    def delete_bookmark(self, bm_id: str) -> bool:
        """Delete a bookmark by ID."""
        before = len(self.store.bookmarks)
        self.store.bookmarks = [b for b in self.store.bookmarks if b.id != bm_id]
        if len(self.store.bookmarks) < before:
            self._save()
            return True
        return False

    def list_bookmarks(self) -> list[Bookmark]:
        return list(self.store.bookmarks)

    def move_bookmarks(
        self,
        bookmark_ids: list[str],
        target_category_id: str,
    ) -> int:
        """Move multiple bookmarks to *target_category_id*.

        Returns the number of bookmarks actually moved.
        """
        if self._find_category(target_category_id) is None:
            logger.warning("Target category %s does not exist", target_category_id)
            return 0

        moved = 0
        ids_set = set(bookmark_ids)
        for bm in self.store.bookmarks:
            if bm.id in ids_set and bm.category_id != target_category_id:
                bm.category_id = target_category_id
                moved += 1

        if moved:
            self._save()
        return moved

    def _find_bookmark(self, bm_id: str) -> Bookmark | None:
        return next((b for b in self.store.bookmarks if b.id == bm_id), None)

    # ------------------------------------------------------------------
    # Import / Export
    # ------------------------------------------------------------------

    def export_json(self) -> str:
        """Serialise the entire store to a JSON string."""
        return self.store.model_dump_json(indent=2)

    def import_json(self, data: str) -> int:
        """Import bookmarks (and optionally categories) from a JSON string.

        Merges into the existing store -- duplicates by ID are skipped.

        Returns the number of bookmarks imported.
        """
        try:
            incoming = BookmarkStore(**json.loads(data))
        except Exception as exc:
            logger.error("Invalid bookmark JSON: %s", exc)
            return 0

        existing_cat_ids = {c.id for c in self.store.categories}
        for cat in incoming.categories:
            if cat.id not in existing_cat_ids:
                self.store.categories.append(cat)
                existing_cat_ids.add(cat.id)

        existing_bm_ids = {b.id for b in self.store.bookmarks}
        imported = 0
        for bm in incoming.bookmarks:
            if bm.id not in existing_bm_ids:
                # Ensure the bookmark's category exists
                if bm.category_id not in existing_cat_ids:
                    bm.category_id = "default"
                self.store.bookmarks.append(bm)
                existing_bm_ids.add(bm.id)
                imported += 1

        if imported:
            self._save()
        logger.info("Imported %d bookmarks", imported)
        return imported

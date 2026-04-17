"""Safe JSON persistence helpers shared by every ``~/.locwarp`` data file
(bookmarks, routes, recent places, settings).

Two invariants both helpers enforce:

* **Never silently discard user data on a load failure.** The previous
  per-file pattern — ``try: read; except: return empty`` — meant any
  transient corruption or schema mismatch made the in-memory state go
  empty; the next write then overwrote the original file with that
  empty state, destroying the user's data for good. These helpers copy
  the corrupt file aside to ``<file>.bak-<timestamp>`` before handing
  the caller an empty result, so the original bytes are always
  recoverable.
* **Every write is atomic.** We serialise to a sibling ``.tmp`` file and
  then ``Path.replace`` it over the target. A crash / power loss / OS
  kill in the middle of the write leaves the original file untouched
  instead of truncated.

Callers import and use ``safe_load_json`` / ``safe_write_json`` directly
so every .locwarp JSON file gets the same protection, not just the one
that already surfaced a data-loss report.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _backup_corrupt(path: Path, reason: str) -> None:
    """Copy the current file contents to ``<name>.bak-<UTC timestamp>``.

    Best-effort: if the backup itself fails (disk full, permission
    error) we just log and move on. The caller will hand the user an
    empty in-memory state either way, but at least this function has
    made its best attempt to preserve the bytes.
    """
    try:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        backup = path.with_suffix(path.suffix + f".bak-{ts}")
        backup.write_bytes(path.read_bytes())
        logger.error(
            "failed to read %s (%s); backed up corrupt file to %s and starting empty",
            path.name, reason, backup.name,
        )
    except Exception:
        logger.error(
            "failed to read %s (%s) AND could not back it up; starting empty",
            path.name, reason,
        )


def safe_load_json(path: Path) -> Any:
    """Load a JSON file. Returns ``None`` if the file doesn't exist
    (first-run / fresh install). Raises nothing on parse failure;
    instead copies the corrupt file aside and returns ``None`` so the
    caller can fall back to its own default in-memory state.
    """
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        _backup_corrupt(path, str(exc))
        return None


def safe_write_json(path: Path, payload: Any, *, indent: int = 2) -> bool:
    """Write ``payload`` as JSON to ``path`` atomically.

    Serialises to a ``<name>.tmp`` sibling first and then renames over
    the real file. Returns ``True`` on success, ``False`` on failure
    (callers that care can react, most don't).
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps(payload, ensure_ascii=False, indent=indent)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(body, encoding="utf-8")
        tmp.replace(path)
        return True
    except Exception as exc:
        logger.error("failed to write %s: %s", path.name, exc)
        return False

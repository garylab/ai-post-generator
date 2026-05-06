"""DB-backed runtime settings with in-process cache.

Each known setting has a default value and a caster (e.g. float, int, bool).
On first access the value is read from the `settings` table; if missing, the
default is written to the DB and used. Edits via the admin dashboard call
`invalidate()` so the next read picks up the new value.
"""
from __future__ import annotations

from typing import Any, Callable

from src.storage import database as db


# ─── Setting registry ─────────────────────────────────────────────
# (key, default, caster, description)
_REGISTRY: dict[str, tuple[Any, Callable[[str], Any], str]] = {
    "intent_cluster_similarity": (
        0.70, float,
        "Cosine similarity (0–1) for grouping intents into a cluster.",
    ),
    "intent_dedup_similarity": (
        0.88, float,
        "Cosine similarity above which a new intent is treated as a duplicate of an existing one.",
    ),
    "content_similar_threshold": (
        0.85, float,
        "Cosine similarity above which a researched title is treated as duplicate of existing content.",
    ),
}

_CACHE: dict[str, Any] = {}


def _cast_bool(s: str) -> bool:
    return str(s).strip().lower() in ("1", "true", "yes", "on")


async def get(key: str) -> Any:
    """Return the parsed value for `key`. Seeds DB with default on first miss."""
    if key in _CACHE:
        return _CACHE[key]
    if key not in _REGISTRY:
        raise KeyError(f"Unknown setting: {key}")
    default, caster, description = _REGISTRY[key]

    row = await db.fetch_setting(key)
    if row is None:
        await db.upsert_setting(key, str(default), description)
        value = default
    else:
        try:
            value = caster(row.value) if row.value != "" else default
        except (TypeError, ValueError):
            value = default

    _CACHE[key] = value
    return value


def invalidate(key: str | None = None) -> None:
    if key is None:
        _CACHE.clear()
    else:
        _CACHE.pop(key, None)


async def seed_known() -> None:
    """Force-seed every registered key so it appears on the settings page."""
    for key in _REGISTRY:
        await get(key)

"""DB-backed prompt loader with in-memory cache.

Prompts are stored in the `settings` table (alongside other admin-editable
key-value pairs) and editable via the admin dashboard. On first access, the
hardcoded fallback in `src/content/prompts.py` is used to seed the row so the
system works on a fresh install.
"""
from __future__ import annotations

from src.storage import database as db


# In-process cache: { key: body }. Cleared via invalidate() after edits.
_CACHE: dict[str, str] = {}


async def get_prompt(key: str, fallback: str, name: str = "", description: str = "") -> str:
    """Return the prompt body for `key`. Seeds the settings table on first miss."""
    if key in _CACHE:
        return _CACHE[key]

    row = await db.fetch_setting(key)
    if row is None:
        # Seed from the hardcoded fallback so a fresh install works.
        await db.upsert_setting(key, fallback, description)
        body = fallback
    else:
        body = row.value or fallback

    _CACHE[key] = body
    return body


def invalidate(key: str | None = None) -> None:
    """Drop a single key (or the whole cache) so the next read re-fetches."""
    if key is None:
        _CACHE.clear()
    else:
        _CACHE.pop(key, None)

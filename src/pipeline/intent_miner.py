"""Mine user search intents from multiple data sources.

Sources:
- Google Autocomplete: what people start typing
- People Also Ask: questions people ask
- Google Forums (via SerpAPI): pain signals from Reddit, Quora, etc.
- Google Trends: rising / breakout queries
"""
from __future__ import annotations

import asyncio

from src.storage.models import RawIntent
from src.utils import serpapi_client


def _clean_title(s: str) -> str:
    """Trim leading/trailing whitespace only — keep whatever SerpAPI returns."""
    return (s or "").strip()
from loguru import logger as log


def _slugify_url(text: str) -> str:
    import re
    slug = text.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    return slug[:80].strip("-")


async def _mine_autocomplete(seed: str) -> list[RawIntent]:
    data = await serpapi_client.google_autocomplete(seed)
    results: list[RawIntent] = []
    for sug in data.get("suggestions", [])[:10]:
        value = sug.get("value", "").strip()
        if not value:
            continue
        results.append(RawIntent(
            title=_clean_title(value),
            source="autocomplete",
            source_url=f"autocomplete://{_slugify_url(value)}",
            volume_hint=sug.get("relevance", 500),
        ))
    return results


async def _mine_paa(seed: str) -> list[RawIntent]:
    data = await serpapi_client.people_also_ask(seed)
    results: list[RawIntent] = []
    for item in data.get("related_questions", []):
        question = item.get("question", "").strip()
        if not question:
            continue
        # Extract snippet from first text_block paragraph
        snippet = ""
        for block in item.get("text_blocks", []):
            if block.get("type") == "paragraph" and block.get("snippet"):
                snippet = block["snippet"][:300]
                break
        # First reference link serves as source_url
        refs = item.get("references", [])
        source_url = refs[0].get("link", "") if refs else ""
        results.append(RawIntent(
            title=_clean_title(question),
            source="paa",
            source_url=source_url,
            snippet=snippet,
            volume_hint=8,
        ))
    return results


def _parse_engagement(displayed_meta: str) -> int:
    """Extract comment/answer count from strings like '40+ comments · 14 years ago'."""
    if not displayed_meta:
        return 0
    import re
    m = re.search(r"(\d+)\+?\s*(?:comment|answer|repl)", displayed_meta, re.IGNORECASE)
    return int(m.group(1)) if m else 0


async def _mine_forums(seed: str) -> list[RawIntent]:
    data = await serpapi_client.google_forums(seed)
    results: list[RawIntent] = []
    for item in data.get("organic_results", [])[:10]:
        title = item.get("title", "").strip()
        if not title:
            continue
        engagement = _parse_engagement(item.get("displayed_meta", ""))
        forum_source = item.get("source", "")
        results.append(RawIntent(
            title=_clean_title(title),
            source="forums",
            source_url=item.get("link", ""),
            snippet=(item.get("snippet", "") or "")[:300],
            volume_hint=min(engagement / 5, 10) if engagement else 3,
            engagement=engagement,
        ))
        # Sitelinks are bonus intents (related threads from the same forum)
        for sl in (item.get("sitelinks", {}).get("list", []) or []):
            sl_title = sl.get("title", "").strip()
            if not sl_title:
                continue
            sl_engagement = sl.get("answer_count", 0) or 0
            results.append(RawIntent(
                title=_clean_title(sl_title),
                source="forums",
                source_url=sl.get("link", ""),
                snippet="",
                volume_hint=min(sl_engagement / 5, 10) if sl_engagement else 2,
                engagement=sl_engagement,
            ))
    return results


async def fetch_trends(
    seed: str, rising_limit: int = 8, top_limit: int = 5,
) -> tuple[list[tuple[str, float]], list[RawIntent]]:
    """Single Trends fetch that produces both new seed-keyword candidates AND raw intents.

    Returns:
      (queries, intents) — queries is [(query, score), ...] for the brand_keywords table;
      intents is [RawIntent, ...] with source='trends' for the intent pipeline.

    Score semantics:
      - rising: percentage growth (e.g. 450 for "+450%")
      - top:    0–100 popularity index
    """
    data = await serpapi_client.google_trends(seed)
    related = data.get("related_queries") or {}

    queries: list[tuple[str, float]] = []
    intents: list[RawIntent] = []

    for item in (related.get("rising") or [])[:rising_limit]:
        q = (item.get("query") or "").strip()
        if not q:
            continue
        score = float(item.get("extracted_value", 0) or 0)
        queries.append((q, score))
        intents.append(RawIntent(
            title=q,
            source="trends",
            source_url=f"trends://{_slugify_url(q)}",
            volume_hint=min(score / 10, 10) if score else 5,
        ))

    for item in (related.get("top") or [])[:top_limit]:
        q = (item.get("query") or "").strip()
        if not q:
            continue
        score = float(item.get("extracted_value", 0) or 0)
        queries.append((q, score))
        intents.append(RawIntent(
            title=q,
            source="trends",
            source_url=f"trends://{_slugify_url(q)}",
            volume_hint=min(score / 10, 10) if score else 3,
        ))

    return queries, intents


async def mine_intents(seeds: list[str]) -> list[RawIntent]:
    """Mine user intents from all sources using the given seed keywords.

    Returns a flat list of raw intents (not yet deduped or clustered).
    """
    tasks = []
    for seed in seeds:
        tasks.append(_mine_autocomplete(seed))
        tasks.append(_mine_paa(seed))
        tasks.append(_mine_forums(seed))

    raw_batches = await asyncio.gather(*tasks, return_exceptions=True)

    all_intents: list[RawIntent] = []
    for i, batch in enumerate(raw_batches):
        if isinstance(batch, Exception):
            log.warning("Intent miner task {} failed: {}", i, batch)
            continue
        all_intents.extend(batch)

    log.info("Mined {} raw intents from {} seeds × 3 sources (autocomplete/paa/forums)",
             len(all_intents), len(seeds))
    return all_intents

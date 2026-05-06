from __future__ import annotations

import json
import re

from src.content.prompts import HUMANIZE_SYSTEM
from src.content.prompt_store import get_prompt
from src.storage.models import ContentPackage
from src.utils.ai_client import chat_claude
from loguru import logger as log

_LINK_RE = re.compile(r"<a\s+(?![^>]*rel=)", re.IGNORECASE)
_SOURCES_SECTION_RE = re.compile(
    r"(<h2[^>]*>\s*(?:References|Sources|Citations)\s*</h2>.*)",
    re.IGNORECASE | re.DOTALL,
)


def _enforce_nofollow(html: str) -> str:
    return _LINK_RE.sub(
        '<a rel="nofollow noopener noreferrer" target="_blank" ', html,
    )


def _split_sources(html: str) -> tuple[str, str]:
    """Split article body from Sources section so we can protect it."""
    m = _SOURCES_SECTION_RE.search(html)
    if m:
        return html[:m.start()], m.group(1)
    return html, ""


async def humanize(pkg: ContentPackage) -> ContentPackage:
    """Rewrite content to remove AI patterns and add human texture."""

    # Protect Sources section from humanization
    body_html, sources_section = _split_sources(pkg.article_html)

    payload = json.dumps({
        "article_html": body_html,
        "medium_article": pkg.medium_article,
        "social_posts": pkg.social_posts,
        "social_posts_variant_b": pkg.social_posts_variant_b,
    })

    system_prompt = await get_prompt(
        "prompt_humanize_system", HUMANIZE_SYSTEM,
        name="Humanize pass (Claude system)",
        description="System prompt for the humanization rewrite step.",
    )
    raw = await chat_claude(
        user_message=(
            "Rewrite this content to kill all AI patterns. Be aggressive — "
            "if something sounds like an AI wrote it, change it. "
            "Keep all inline <a href> citation links exactly as-is — do not introduce [N] / <sup> markers. "
            "Return ONLY valid JSON, no markdown fences:\n\n" + payload
        ),
        system=system_prompt,
        max_tokens=10000,
        temperature=0.8,
    )

    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0]

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        log.warning("Humanize parse failed for '{}', keeping original", pkg.article_title)
        return pkg

    if data.get("article_html"):
        humanized_body = _enforce_nofollow(data["article_html"])
        # Strip any Sources section the humanizer might have re-added, use our original
        humanized_body, _ = _split_sources(humanized_body)
        pkg.article_html = humanized_body + sources_section
    if data.get("medium_article"):
        pkg.medium_article = data["medium_article"]
    if data.get("social_posts"):
        pkg.social_posts = data["social_posts"]
    if data.get("social_posts_variant_b"):
        pkg.social_posts_variant_b = data["social_posts_variant_b"]
    pkg.humanized = True

    log.info("Humanized content: '{}'", pkg.article_title)
    return pkg

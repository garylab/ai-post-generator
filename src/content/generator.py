from __future__ import annotations

import json
import re
import uuid
from urllib.parse import urlparse

from src.content.prompts import CONTENT_SYSTEM, build_content_prompt
from src.content.prompt_store import get_prompt
from src.storage.models import ContentPackage, ScoredTopic
from src.utils.ai_client import chat_claude
from loguru import logger as log

_H1_RE = re.compile(r"<h1[^>]*>.*?</h1>\s*", re.IGNORECASE | re.DOTALL)
_MD_TITLE_RE = re.compile(r"^#\s+.+\n+", re.MULTILINE)
_LINK_RE = re.compile(r"<a\s+(?![^>]*rel=)", re.IGNORECASE)
_INLINE_LINK_RE = re.compile(
    r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_SOURCES_SECTION_RE = re.compile(
    r"<h2[^>]*>\s*(?:References|Sources|Citations)\s*</h2>.*",
    re.IGNORECASE | re.DOTALL,
)
_OWN_DOMAINS = {"mockreal.com", "ge-cdn.mockreal.com"}


def _strip_title_from_html(html: str) -> str:
    return _H1_RE.sub("", html, count=1).lstrip()


def _strip_title_from_md(md: str) -> str:
    return _MD_TITLE_RE.sub("", md, count=1).lstrip()


def _enforce_nofollow(html: str) -> str:
    """Add rel="nofollow noopener noreferrer" target="_blank" to any <a> missing rel=."""
    return _LINK_RE.sub(
        '<a rel="nofollow noopener noreferrer" target="_blank" ', html,
    )


def _move_citations_to_end(html: str) -> str:
    """Ensure all external citations are in a Sources section at the end, not inline.

    If Claude already produced a Sources section, extract any remaining inline
    external links from the body and merge them into the Sources list.
    If no Sources section exists, extract all inline external links and build one.
    """
    # Split off any existing Sources section
    sources_match = _SOURCES_SECTION_RE.search(html)
    if sources_match:
        body = html[:sources_match.start()]
        sources_html = html[sources_match.start():]
    else:
        body = html
        sources_html = ""

    # Collect existing sources from the Sources section (ordered, for numbering)
    existing_refs: list[tuple[str, str]] = []  # (normalized_url, label)
    if sources_html:
        for m in _INLINE_LINK_RE.finditer(sources_html):
            existing_refs.append((m.group(1).rstrip("/"), re.sub(r"<[^>]+>", "", m.group(2)).strip()))

    # Unified reference list: existing first, then new ones appended
    all_refs: list[tuple[str, str]] = list(existing_refs)  # (normalized_url, label)
    all_urls: dict[str, int] = {u: i + 1 for i, (u, _) in enumerate(all_refs)}

    def _replace_inline_link(m: re.Match) -> str:
        url = m.group(1)
        text = re.sub(r"<[^>]+>", "", m.group(2)).strip()
        try:
            domain = urlparse(url).netloc.replace("www.", "")
        except Exception:
            domain = ""

        if domain in _OWN_DOMAINS or not url.startswith("http"):
            return m.group(0)

        normalized = url.rstrip("/")
        if normalized not in all_urls:
            all_refs.append((normalized, text))
            all_urls[normalized] = len(all_refs)

        idx = all_urls[normalized]
        return f"{text}<sup>[{idx}]</sup>"

    body = _INLINE_LINK_RE.sub(_replace_inline_link, body)

    new_refs = all_refs[len(existing_refs):]

    if not new_refs and sources_html:
        return body + sources_html

    if not new_refs and not sources_html:
        return body

    new_items = ""
    for norm_url, label in new_refs:
        display = label or urlparse(norm_url).netloc.replace("www.", "")
        new_items += (
            f'<li><a href="{norm_url}" rel="nofollow noopener noreferrer" '
            f'target="_blank">{display}</a></li>'
        )

    if sources_html:
        sources_html = sources_html.replace("</ol>", new_items + "</ol>", 1)
        return body + sources_html
    else:
        return (
            body
            + '<h2>References</h2><ol class="references">'
            + new_items
            + "</ol>"
        )


async def generate(topic: ScoredTopic, research: dict | None = None) -> ContentPackage:
    """Generate a full content package from a scored topic using Claude."""
    user_msg = build_content_prompt(topic.model_dump(), research=research)

    system_prompt = await get_prompt(
        "content_system", CONTENT_SYSTEM,
        name="Content writer (Claude system)",
        description="System prompt for the article-writing stage.",
    )
    raw = await chat_claude(
        user_message=user_msg,
        system=system_prompt,
        max_tokens=8192,
        temperature=0.6,
    )

    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0]

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        log.error("Failed to parse content JSON for topic: {}", topic.title)
        data = {"article_title": topic.title, "article_html": cleaned}

    article_html = _enforce_nofollow(
        _move_citations_to_end(_strip_title_from_html(data.get("article_html", "")))
    )
    medium_article = _strip_title_from_md(data.get("medium_article", ""))

    pkg = ContentPackage(
        content_id=uuid.uuid4().hex[:16],
        topic=topic,
        article_title=data.get("article_title", topic.title),
        outline=data.get("outline", []),
        article_html=article_html,
        medium_article=medium_article,
        social_posts=data.get("social_posts", {}),
        social_posts_variant_b=data.get("social_posts_variant_b", {}),
        seo_keywords=data.get("seo_keywords", []),
        meta_description=data.get("meta_description", ""),
        cta_variant_a=data.get("cta_variant_a", ""),
        cta_variant_b=data.get("cta_variant_b", ""),
    )
    log.info("Generated content: '{}' ({} chars HTML)", pkg.article_title, len(pkg.article_html))
    return pkg

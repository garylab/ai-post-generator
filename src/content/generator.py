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


_SUP_REF_RE = re.compile(r"<sup>\s*\[?\s*\d+\s*\]?\s*</sup>", re.IGNORECASE)
_BARE_NUM_REF_RE = re.compile(r"\s*\[\s*\d+\s*\]")


def _strip_numeric_refs(html: str) -> str:
    """Remove [N] / <sup>[N]</sup> markers — we only want inline link citations."""
    html = _SUP_REF_RE.sub("", html)
    html = _BARE_NUM_REF_RE.sub("", html)
    return html


def _move_citations_to_end(html: str) -> str:
    """Ensure cited external links are summarized in a Sources section at the end.

    Inline <a href> tags stay in place; we just build (or augment) a list at the
    bottom so readers can see all sources at a glance. We never introduce
    superscript reference numbers — this strips any the LLM tried to add.
    """
    html = _strip_numeric_refs(html)

    sources_match = _SOURCES_SECTION_RE.search(html)
    if sources_match:
        body = html[:sources_match.start()]
        sources_html = html[sources_match.start():]
    else:
        body = html
        sources_html = ""

    # URLs already listed in the Sources section
    existing_urls: set[str] = set()
    if sources_html:
        for m in _INLINE_LINK_RE.finditer(sources_html):
            existing_urls.add(m.group(1).rstrip("/"))

    # Collect external inline links from the body, in order, dedup
    body_urls: list[tuple[str, str]] = []  # (normalized_url, label)
    seen: set[str] = set(existing_urls)
    for m in _INLINE_LINK_RE.finditer(body):
        url = m.group(1)
        if not url.startswith("http"):
            continue
        try:
            domain = urlparse(url).netloc.replace("www.", "")
        except Exception:
            domain = ""
        if domain in _OWN_DOMAINS:
            continue
        normalized = url.rstrip("/")
        if normalized in seen:
            continue
        seen.add(normalized)
        text = re.sub(r"<[^>]+>", "", m.group(2)).strip()
        body_urls.append((normalized, text))

    if not body_urls:
        return body + sources_html  # nothing to add; preserve existing list

    new_items = ""
    for norm_url, label in body_urls:
        display = label or urlparse(norm_url).netloc.replace("www.", "")
        new_items += (
            f'<li><a href="{norm_url}" rel="nofollow noopener noreferrer" '
            f'target="_blank">{display}</a></li>'
        )

    if sources_html:
        # Append to whichever list tag the LLM used (ol or ul)
        for closing in ("</ul>", "</ol>"):
            if closing in sources_html:
                sources_html = sources_html.replace(closing, new_items + closing, 1)
                break
        return body + sources_html
    else:
        return (
            body
            + '<h2>Sources</h2><ul class="sources">'
            + new_items
            + "</ul>"
        )


async def generate(
    topic: ScoredTopic,
    research: dict | None = None,
    brand: dict | None = None,
) -> ContentPackage:
    """Generate a full content package from a scored topic using Claude."""
    user_msg = build_content_prompt(topic.model_dump(), research=research, brand=brand)

    system_prompt = await get_prompt(
        "prompt_content_system", CONTENT_SYSTEM,
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

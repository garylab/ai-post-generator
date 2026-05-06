from __future__ import annotations

import re

from src.content.prompts import WECHAT_SYSTEM
from src.content.prompt_store import get_prompt
from src.storage.models import ContentPackage
from src.utils.ai_client import chat_claude
from loguru import logger as log


_STYLE_ATTR = re.compile(
    r'(<\s*(?:p|section|strong)\b[^>]*?)\s+style\s*=\s*"[^"]*"',
    re.IGNORECASE,
)


def _strip_inline_styles(html: str) -> str:
    """Remove inline style attributes from <p>, <section>, and <strong> tags."""
    prev = None
    while prev != html:
        prev = html
        html = _STYLE_ATTR.sub(r"\1", html)
    return html


async def convert_to_wechat(pkg: ContentPackage) -> ContentPackage:
    """Convert the existing article_html into a WeChat Official Account article."""
    if not pkg.article_html:
        return pkg

    user_msg = (
        f"Title: {pkg.article_title}\n\n"
        f"Original article HTML:\n{pkg.article_html}"
    )

    system_prompt = await get_prompt(
        "prompt_wechat_system", WECHAT_SYSTEM,
        name="WeChat converter (Claude system)",
        description="System prompt for converting articles to WeChat format.",
    )
    raw = await chat_claude(
        user_message=user_msg,
        system=system_prompt,
        max_tokens=6000,
        temperature=0.4,
    )

    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

    if cleaned and "<" in cleaned:
        cleaned = _strip_inline_styles(cleaned)
        pkg.wechat_article = cleaned
        log.info("WeChat article generated: '{}' ({} chars)", pkg.article_title, len(cleaned))
    else:
        log.warning("WeChat conversion returned unexpected format for '{}'", pkg.article_title)

    return pkg

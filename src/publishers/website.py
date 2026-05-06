from __future__ import annotations

import re

import httpx
from loguru import logger as log
from tenacity import retry, stop_after_attempt, wait_exponential

from src.publishers.base import BasePublisher, PublishResult
from src.storage.models import ContentPackage


def _slugify(title: str) -> str:
    slug = title.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    return slug[:80].strip("-")


class WebsitePublisher(BasePublisher):
    platform = "website"

    @retry(stop=stop_after_attempt(2), wait=wait_exponential(min=2, max=10))
    async def publish(self, pkg: ContentPackage, cta_variant: str = "a") -> PublishResult:
        api_url = self.creds.get("api_url", "")
        api_key = self.creds.get("api_key", "")
        if not api_url:
            return PublishResult(self.platform, "", False, "No website URL configured")

        cta_html = self._pick_cta(pkg, cta_variant)
        html = pkg.article_html
        if cta_html:
            html += f'\n<div class="cta">{cta_html}</div>'

        payload = {
            "title": pkg.article_title,
            "slug": _slugify(pkg.article_title),
            "content": html,
            "main_image": pkg.featured_image_url,
            "status": "published",
        }

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{api_url}/api/blogs/create",
                json=payload,
                headers={"Content-Type": "application/json", "X-API-Key": api_key},
            )
            resp.raise_for_status()
            data = resp.json()

        slug = data.get("slug", payload["slug"])
        url = f"{api_url}/blog/{slug}"
        log.info("Published to website: {}", url)
        return PublishResult(self.platform, url, True, post_body=html)

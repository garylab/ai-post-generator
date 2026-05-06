from __future__ import annotations

import httpx
from loguru import logger as log
from tenacity import retry, stop_after_attempt, wait_exponential

from src.publishers.base import BasePublisher, PublishResult
from src.storage.models import ContentPackage


class MediumPublisher(BasePublisher):
    platform = "medium"

    @retry(stop=stop_after_attempt(2), wait=wait_exponential(min=2, max=10))
    async def publish(self, pkg: ContentPackage, cta_variant: str = "a") -> PublishResult:
        api_token = self.creds.get("api_token", "")
        author_id = self.creds.get("author_id", "")
        if not api_token or not author_id:
            return PublishResult(self.platform, "", False, "Medium not configured")

        body = pkg.medium_article or pkg.article_html
        body += f"\n\n---\n\n{self._pick_cta(pkg, cta_variant)}"

        payload = {
            "title": pkg.article_title,
            "contentFormat": "markdown" if pkg.medium_article else "html",
            "content": body,
            "tags": pkg.seo_keywords[:5],
            "publishStatus": "public",
        }

        url = f"https://api.medium.com/v1/users/{author_id}/posts"
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                url, json=payload, headers={"Authorization": f"Bearer {api_token}"},
            )
            resp.raise_for_status()
            data = resp.json()

        post_url = data.get("data", {}).get("url", "")
        log.info("Published to Medium: {}", post_url)
        return PublishResult(self.platform, post_url, True, post_body=body)

from __future__ import annotations

import abc
from dataclasses import dataclass

from src.storage.models import ContentPackage


@dataclass
class PublishResult:
    platform: str
    url: str
    success: bool
    error: str = ""
    post_body: str = ""


class BasePublisher(abc.ABC):
    platform: str = ""

    def __init__(self, credentials: dict | None = None, display_name: str = ""):
        self.creds = credentials or {}
        self.display_name = display_name

    @abc.abstractmethod
    async def publish(self, pkg: ContentPackage, cta_variant: str = "A") -> PublishResult:
        ...

    def _pick_cta(self, pkg: ContentPackage, variant: str) -> str:
        return pkg.cta_variant_a if variant.upper() == "A" else pkg.cta_variant_b

    def _pick_social(self, pkg: ContentPackage, variant: str) -> dict:
        return pkg.social_posts if variant.upper() == "A" else pkg.social_posts_variant_b

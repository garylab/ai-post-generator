from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Database
    db_url: str = "postgresql://postgres:@localhost:5432/ai-post-generator"

    # Timezone
    generic_timezone: str = "Asia/Dubai"

    # AI
    anthropic_api_key: str = ""
    openai_api_key: str = ""

    # Data sources
    serpapi_key: str = ""
    serper_api_key: str = ""

    # Telegram
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # Publishing
    website_api_url: str = ""
    website_api_key: str = ""
    medium_api_token: str = ""
    medium_author_id: str = ""
    linkedin_access_token: str = ""
    linkedin_person_urn: str = ""
    facebook_page_id: str = ""
    facebook_access_token: str = ""

    # Images
    pexels_api_key: str = ""
    r2_endpoint: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_bucket: str = ""
    r2_public_url: str = ""

    # Dashboard
    dashboard_webhook_url: str = ""

    # App
    app_port: int = 8000
    session_secret: str = "change-me-please-set-a-long-random-string"
    pipeline_interval_hours: int = 6
    metrics_hour: int = 3

    # Content generation
    max_articles_per_run: int = 3
    max_content_per_cluster: int = 8

    # Intent mining
    seed_keywords: str = "AI interview,mock interview,job interview tips,career change,tech layoffs,AI hiring,resume optimization,salary negotiation,remote work tips"

    # Rate limiting
    max_concurrent_api: int = 5
    max_concurrent_ai: int = 2

    # Auto-approve drafts without Telegram (useful for local debugging)
    auto_approve: bool = False

    @property
    def dsn(self) -> str:
        return self.db_url


settings = Settings()




BANNED_PHRASES = [
    # Classic AI filler
    "in today's rapidly evolving", "it's worth noting", "let's dive in",
    "in this article we will", "landscape", "leverage", "navigate",
    "unlock", "empower", "delve", "tapestry", "holistic", "game-changer",
    "paradigm shift", "synergy", "cutting-edge", "state-of-the-art",
    "in conclusion", "to sum up", "in summary", "as we've seen",
    "it is important to note", "furthermore", "moreover", "certainly",
    "absolutely", "without a doubt", "at the end of the day",
    "when it comes to", "in order to", "the fact that",
    # Clickbait / listicle patterns
    "that actually work", "you need to know", "nobody talks about",
    "the truth about", "here's why", "ultimate guide",
    "real examples inside", "not just", "you won't believe",
    "everything you need", "a comprehensive guide", "deep dive",
    "revolutionize", "transform your", "master the art",
    "secrets to", "proven strategies", "essential tips",
    # AI-specific structural tells
    "here's the thing", "but here's what", "the reality is",
    "let's be honest", "the good news", "welcome to",
    "spoiler alert", "plot twist", "fast forward to",
    "here's the kicker", "the bottom line", "let that sink in",
    "full stop", "period.", "every time.",
    # Fake authority phrases
    "studies show", "research indicates", "experts agree",
    "according to recent studies", "data suggests",
    # AI emotional manipulation
    "whether you like it or not", "and that's okay",
    "and it's not even close", "and it's only getting",
    "the question isn't whether", "it's not a matter of if",
]

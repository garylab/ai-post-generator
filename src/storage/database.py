from __future__ import annotations

import enum as _enum
import json
import uuid as _uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from src.config import settings
from src.storage.models import (
    ContentRow,
    ContentStatus,
    IntentClusterRow,
    IntentRow,
    PerformanceRow,
    PublishLogRow,
    Prompt,
    PromptRow,
    Role,
    RoleRow,
    RoleSocialAccount,
    RoleSocialAccountRow,
    SeedKeyword,
    SeedKeywordRow,
    Setting,
    SettingRow,
    User,
    UserRow,
)


# ── Engine / Session ────────────────────────────────────────────

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


async def init_db() -> None:
    global _engine, _session_factory
    url = settings.dsn.replace("postgresql://", "postgresql+asyncpg://")
    _engine = create_async_engine(url, pool_size=10, pool_pre_ping=True)
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)


async def close_db() -> None:
    global _engine, _session_factory
    if _engine:
        await _engine.dispose()
        _engine = None
        _session_factory = None


def get_session() -> AsyncSession:
    if _session_factory is None:
        raise RuntimeError("Database not initialized — call init_db() first")
    return _session_factory()


# ── Helpers ─────────────────────────────────────────────────────

def _vec_literal(emb: list[float] | Any) -> str:
    """Serialize an embedding to a PostgreSQL vector literal for text() queries."""
    if hasattr(emb, "tolist"):
        emb = emb.tolist()
    return "[" + ",".join(str(x) for x in emb) + "]"


def _to_dict(row: Any) -> dict:
    """Convert an ORM model instance to a plain dict."""
    d: dict[str, Any] = {}
    for c in row.__class__.__table__.columns:
        val = getattr(row, c.key)
        if isinstance(val, _enum.Enum):
            val = val.value
        elif isinstance(val, Decimal):
            val = float(val)
        d[c.name] = val
    return d


# ── Content Stage Helpers ──────────────────────────────────────

async def insert_queued_content(
    content_id: str,
    title: str,
    cluster: str,
    score: float,
    intent_id: int | None,
    title_embedding: list[float] | None = None,
    role_id: int | None = None,
) -> None:
    """Create a content row at the 'queued' stage — pending research."""
    async with get_session() as session:
        stmt = (
            pg_insert(ContentRow)
            .values(
                content_id=content_id,
                intent_id=intent_id,
                role_id=role_id,
                title=title,
                title_embedding=title_embedding,
                cluster=cluster,
                score=score,
                research_data={},
                status="queued",
                priority="medium",
            )
            .on_conflict_do_nothing(index_elements=["content_id"])
        )
        await session.execute(stmt)
        await session.commit()


async def insert_researched_content(
    content_id: str,
    title: str,
    cluster: str,
    score: float,
    intent_id: int | None,
    research_data: dict,
    title_embedding: list[float] | None = None,
    role_id: int | None = None,
) -> None:
    """Create a content row at the 'researched' stage with research data persisted."""
    async with get_session() as session:
        stmt = (
            pg_insert(ContentRow)
            .values(
                content_id=content_id,
                intent_id=intent_id,
                role_id=role_id,
                title=title,
                title_embedding=title_embedding,
                cluster=cluster,
                score=score,
                research_data=research_data,
                status="researched",
                priority="medium",
            )
            .on_conflict_do_nothing(index_elements=["content_id"])
        )
        await session.execute(stmt)
        await session.commit()


async def fetch_content_by_status(status: str, limit: int = 10) -> list[dict]:
    """Fetch content rows at a given pipeline stage."""
    async with get_session() as session:
        result = await session.execute(
            text("""
                SELECT * FROM content
                WHERE status = :status
                ORDER BY score DESC, created_at ASC
                LIMIT :lim
            """),
            {"status": status, "lim": limit},
        )
        return [dict(r) for r in result.mappings().all()]


async def update_content_stage(content_id: str, new_status: str, **fields: Any) -> None:
    """Atomically advance a content row to the next stage and update fields."""
    async with get_session() as session:
        values: dict[str, Any] = {"status": new_status, **fields}
        if new_status == "approved":
            values["approved_at"] = func.now()
        await session.execute(
            update(ContentRow).where(ContentRow.content_id == content_id).values(**values)
        )
        await session.commit()


# ── Content CRUD ───────────────────────────────────────────────

async def find_similar_content(
    embedding: list[float],
    threshold: float = 0.85,
    days: int = 60,
) -> dict | None:
    """Check if a semantically similar article already exists in the content table."""
    async with get_session() as session:
        result = await session.execute(
            text("""
                SELECT content_id, title, 1 - (title_embedding <=> CAST(:vec AS vector)) AS similarity
                FROM content
                WHERE created_at > NOW() - :days * INTERVAL '1 day'
                  AND title_embedding IS NOT NULL
                ORDER BY title_embedding <=> CAST(:vec AS vector)
                LIMIT 1
            """),
            {"vec": _vec_literal(embedding), "days": days},
        )
        row = result.mappings().first()
        if row and float(row["similarity"]) >= threshold:
            return {
                "content_id": row["content_id"],
                "title": row["title"],
                "similarity": float(row["similarity"]),
            }
        return None


async def insert_draft(
    content_id: str,
    title: str,
    cluster: str,
    score: float,
    article_html: str,
    medium_article: str,
    seo_keywords: list[str],
    meta_description: str,
    social_posts: dict,
    social_posts_variant_b: dict,
    cta_a: str,
    cta_b: str,
    outline: list[str],
    suggested_angle: str,
    priority: str,
    image_url: str = "",
    wechat_article: str = "",
    title_embedding: list[float] | None = None,
    intent_id: int | None = None,
) -> None:
    async with get_session() as session:
        stmt = (
            pg_insert(ContentRow)
            .values(
                content_id=content_id,
                intent_id=intent_id,
                title=title,
                title_embedding=title_embedding,
                cluster=cluster,
                score=score,
                article_html=article_html,
                medium_article=medium_article,
                wechat_article=wechat_article or None,
                seo_keywords=seo_keywords,
                meta_description=meta_description,
                social_posts=social_posts,
                social_posts_variant_b=social_posts_variant_b,
                cta_variant_a=cta_a,
                cta_variant_b=cta_b,
                outline=outline,
                suggested_angle=suggested_angle,
                priority=priority,
                image_url=image_url,
                status="draft",
            )
            .on_conflict_do_nothing(index_elements=["content_id"])
        )
        await session.execute(stmt)
        await session.commit()


async def update_content_status(content_id: str, status: str) -> None:
    async with get_session() as session:
        values: dict[str, Any] = {"status": status}
        if status == "approved":
            values["approved_at"] = func.now()
        await session.execute(
            update(ContentRow).where(ContentRow.content_id == content_id).values(**values)
        )
        await session.commit()


async def insert_publish_log(
    content_id: str,
    platform: str,
    url: str,
    cta_variant: str,
    post_body: str = "",
) -> None:
    async with get_session() as session:
        await session.execute(
            insert(PublishLogRow).values(
                content_id=content_id,
                platform=platform,
                published_url=url,
                cta_variant=cta_variant,
                post_body=post_body,
            )
        )
        await session.commit()


async def upsert_performance(
    content_id: str,
    platform: str,
    impressions: int,
    clicks: int,
    signups: int,
    ctr: float,
    conversion_rate: float,
) -> None:
    async with get_session() as session:
        stmt = pg_insert(PerformanceRow).values(
            content_id=content_id,
            platform=platform,
            impressions=impressions,
            clicks=clicks,
            signups=signups,
            ctr=ctr,
            conversion_rate=conversion_rate,
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_perf_content_platform_period",
            set_={
                "impressions": stmt.excluded.impressions,
                "clicks": stmt.excluded.clicks,
                "signups": stmt.excluded.signups,
                "ctr": stmt.excluded.ctr,
                "conversion_rate": stmt.excluded.conversion_rate,
                "measured_at": func.now(),
            },
        )
        await session.execute(stmt)
        await session.commit()


# ── Queries ────────────────────────────────────────────────────

async def fetch_cluster_feedback() -> list[dict]:
    async with get_session() as session:
        result = await session.execute(
            text("""
                SELECT c.cluster, COUNT(*) AS total_posts,
                       COALESCE(AVG(p.ctr),0) AS avg_ctr,
                       COALESCE(AVG(p.conversion_rate),0) AS avg_conversion
                FROM content c
                LEFT JOIN performance p ON c.content_id = p.content_id
                WHERE c.status IN ('approved','published')
                  AND c.created_at > NOW() - INTERVAL '30 days'
                GROUP BY c.cluster
            """)
        )
        return [dict(r) for r in result.mappings().all()]


async def fetch_top_performers(limit: int = 10) -> list[dict]:
    async with get_session() as session:
        result = await session.execute(
            text("""
                SELECT c.content_id, c.title, c.cluster, c.suggested_angle,
                       COALESCE(c.seo_keywords, '[]') AS seo_keywords,
                       ROUND(AVG(p.ctr)::numeric, 2) AS avg_ctr,
                       ROUND(AVG(p.conversion_rate)::numeric, 2) AS avg_conv,
                       SUM(p.clicks) AS total_clicks,
                       SUM(p.signups) AS total_signups,
                       COUNT(DISTINCT pl.platform) AS platforms_published
                FROM content c
                JOIN performance p ON c.content_id = p.content_id
                JOIN publish_logs pl ON c.content_id = pl.content_id
                WHERE c.status IN ('approved','published')
                  AND (p.ctr > 1.5 OR p.conversion_rate > 1.0)
                  AND c.created_at > NOW() - INTERVAL '60 days'
                GROUP BY c.content_id, c.title, c.cluster, c.suggested_angle, c.seo_keywords
                ORDER BY AVG(p.conversion_rate) DESC, AVG(p.ctr) DESC
                LIMIT :lim
            """),
            {"lim": limit},
        )
        return [dict(r) for r in result.mappings().all()]


async def fetch_recent_publishes(days: int = 7) -> list[dict]:
    async with get_session() as session:
        result = await session.execute(
            text("""
                SELECT pl.content_id, c.title, c.cluster, c.score,
                       pl.platform, pl.cta_variant, pl.published_at,
                       COALESCE(p.ctr,0) AS ctr,
                       COALESCE(p.conversion_rate,0) AS conversion_rate,
                       COALESCE(p.clicks,0) AS clicks,
                       COALESCE(p.signups,0) AS signups
                FROM publish_logs pl
                JOIN content c ON pl.content_id = c.content_id
                LEFT JOIN performance p
                    ON pl.content_id = p.content_id AND pl.platform = p.platform
                WHERE pl.published_at > NOW() - :days * INTERVAL '1 day'
                ORDER BY pl.published_at DESC
            """),
            {"days": days},
        )
        return [dict(r) for r in result.mappings().all()]


async def fetch_low_ctr_content(threshold: float = 1.0, limit: int = 5) -> list[dict]:
    async with get_session() as session:
        result = await session.execute(
            text("""
                SELECT c.content_id, c.title, c.cluster, c.article_html,
                       c.cta_variant_a, c.cta_variant_b,
                       AVG(p.ctr) AS avg_ctr
                FROM content c
                JOIN performance p ON c.content_id = p.content_id
                WHERE c.status = 'approved' AND p.ctr < :threshold
                  AND c.created_at > NOW() - INTERVAL '30 days'
                GROUP BY c.content_id, c.title, c.cluster, c.article_html,
                         c.cta_variant_a, c.cta_variant_b
                ORDER BY AVG(p.ctr) ASC
                LIMIT :lim
            """),
            {"threshold": threshold, "lim": limit},
        )
        return [dict(r) for r in result.mappings().all()]


async def update_regenerated(
    content_id: str,
    article_html: str,
    social_posts: dict,
    title: str | None = None,
) -> None:
    async with get_session() as session:
        stmt = (
            update(ContentRow)
            .where(ContentRow.content_id == content_id)
            .values(
                article_html=article_html,
                social_posts=social_posts,
                iteration_count=ContentRow.iteration_count + 1,
            )
        )
        if title:
            stmt = stmt.values(title=title)
        await session.execute(stmt)
        await session.commit()


async def get_pending_approval(content_id: str) -> dict | None:
    async with get_session() as session:
        result = await session.execute(
            select(ContentRow).where(
                ContentRow.content_id == content_id,
                ContentRow.status == ContentStatus.draft,
            )
        )
        row = result.scalar_one_or_none()
        return _to_dict(row) if row else None


async def title_exists(title: str, days: int = 30) -> bool:
    async with get_session() as session:
        result = await session.execute(
            text("""
                SELECT 1 FROM content
                WHERE LOWER(title) = LOWER(:title)
                  AND created_at > NOW() - :days * INTERVAL '1 day'
                LIMIT 1
            """),
            {"title": title, "days": days},
        )
        return result.first() is not None


async def fetch_recent_titles(days: int = 30) -> set[str]:
    async with get_session() as session:
        result = await session.execute(
            text(
                "SELECT LOWER(title) AS t FROM content"
                " WHERE created_at > NOW() - :days * INTERVAL '1 day'"
            ),
            {"days": days},
        )
        return {r[0] for r in result.all()}


async def fetch_ab_results() -> list[dict]:
    async with get_session() as session:
        result = await session.execute(
            text("""
                SELECT pl.cta_variant,
                       COUNT(*) AS total_publishes,
                       COALESCE(AVG(p.ctr), 0) AS avg_ctr,
                       COALESCE(AVG(p.conversion_rate), 0) AS avg_conv,
                       COALESCE(SUM(p.clicks), 0) AS total_clicks,
                       COALESCE(SUM(p.signups), 0) AS total_signups
                FROM publish_logs pl
                LEFT JOIN performance p
                    ON pl.content_id = p.content_id AND pl.platform = p.platform
                WHERE pl.published_at > NOW() - INTERVAL '30 days'
                GROUP BY pl.cta_variant
            """)
        )
        return [dict(r) for r in result.mappings().all()]


async def fetch_content(content_id: str) -> dict | None:
    """Fetch a full content row as a dict, or None if not found."""
    async with get_session() as session:
        result = await session.execute(
            select(ContentRow).where(ContentRow.content_id == content_id)
        )
        row = result.scalar_one_or_none()
        return _to_dict(row) if row else None


async def find_related_published(
    embedding: list[float],
    exclude_id: str = "",
    limit: int = 3,
) -> list[dict]:
    """Find similar published content for internal linking."""
    async with get_session() as session:
        result = await session.execute(
            text("""
                SELECT content_id, title, 1 - (title_embedding <=> CAST(:vec AS vector)) AS similarity
                FROM content
                WHERE status IN ('approved', 'published')
                  AND title_embedding IS NOT NULL
                  AND content_id != :exclude
                ORDER BY title_embedding <=> CAST(:vec AS vector)
                LIMIT :lim
            """),
            {"vec": _vec_literal(embedding), "exclude": exclude_id, "lim": limit},
        )
        return [
            {"content_id": r["content_id"], "title": r["title"], "similarity": float(r["similarity"])}
            for r in result.mappings().all()
            if float(r["similarity"]) >= 0.5
        ]


# ── Intents & Intent Clusters ─────────────────────────────────

async def fetch_clusters_with_centroids(role_id: int | None = None) -> list[dict]:
    """Return all clusters (optionally per-role) with their stored centroid embeddings."""
    where = "WHERE centroid_embedding IS NOT NULL"
    params: dict = {}
    if role_id is not None:
        where += " AND role_id = :rid"
        params["rid"] = role_id
    async with get_session() as session:
        result = await session.execute(
            text(f"""
                SELECT id, slug, name, role_id, intent_count, covered_count,
                       centroid_embedding::text AS centroid_str
                FROM intent_clusters
                {where}
            """),
            params,
        )
        out: list[dict] = []
        for row in result.mappings().all():
            d = dict(row)
            # Parse pgvector text "[0.1,0.2,...]" → list[float]
            s = d.pop("centroid_str", "")
            if s and s.startswith("["):
                d["centroid"] = [float(x) for x in s[1:-1].split(",") if x]
            else:
                d["centroid"] = []
            out.append(d)
        return out


async def update_cluster_centroid(
    cluster_id: int, centroid: list[float], intent_count: int,
) -> None:
    """Replace the cluster's centroid and intent_count after attaching new intents."""
    async with get_session() as session:
        await session.execute(
            update(IntentClusterRow)
            .where(IntentClusterRow.id == cluster_id)
            .values(
                centroid_embedding=centroid,
                intent_count=intent_count,
                updated_at=func.now(),
            )
        )
        await session.commit()


async def insert_intent_cluster(
    name: str,
    slug: str,
    centroid_embedding: list[float] | None = None,
    intent_count: int = 0,
    priority_score: float = 0,
    role_id: int | None = None,
) -> int:
    """Insert a new intent cluster, returns the cluster id."""
    async with get_session() as session:
        result = await session.execute(
            insert(IntentClusterRow)
            .values(
                name=name,
                slug=slug,
                centroid_embedding=centroid_embedding,
                intent_count=intent_count,
                priority_score=priority_score,
                role_id=role_id,
            )
            .returning(IntentClusterRow.id)
        )
        cid = result.scalar_one()
        await session.commit()
        return cid


async def update_intent_cluster_pillar(
    cluster_id: int,
    pillar_intent_id: int,
) -> None:
    async with get_session() as session:
        await session.execute(
            update(IntentClusterRow)
            .where(IntentClusterRow.id == cluster_id)
            .values(pillar_intent_id=pillar_intent_id)
        )
        await session.commit()


async def insert_intent(
    title: str,
    embedding: list[float] | None,
    source: str,
    source_url: str = "",
    snippet: str = "",
    volume_hint: float = 0,
    priority_score: float = 0,
    cluster_id: int | None = None,
    is_pillar: bool = False,
    batch_id: str = "",
    role_id: int | None = None,
) -> int:
    """Insert a new intent, returns the intent id."""
    async with get_session() as session:
        vals: dict[str, Any] = dict(
            title=title,
            embedding=embedding,
            source=source,
            source_url=source_url or None,
            snippet=snippet,
            volume_hint=volume_hint,
            priority_score=priority_score,
            cluster_id=cluster_id,
            is_pillar=is_pillar,
            role_id=role_id,
        )
        if batch_id:
            vals["batch_id"] = _uuid.UUID(batch_id)
        result = await session.execute(
            insert(IntentRow).values(**vals).returning(IntentRow.id)
        )
        iid = result.scalar_one()
        await session.commit()
        return iid


async def find_similar_intent(
    embedding: list[float],
    threshold: float = 0.88,
    days: int = 90,
) -> dict | None:
    """Find an existing intent whose embedding is within threshold."""
    async with get_session() as session:
        result = await session.execute(
            text("""
                SELECT id, title, 1 - (embedding <=> CAST(:vec AS vector)) AS similarity
                FROM intents
                WHERE created_at > NOW() - :days * INTERVAL '1 day'
                  AND embedding IS NOT NULL
                ORDER BY embedding <=> CAST(:vec AS vector)
                LIMIT 1
            """),
            {"vec": _vec_literal(embedding), "days": days},
        )
        row = result.mappings().first()
        if row and float(row["similarity"]) >= threshold:
            return {"id": row["id"], "title": row["title"], "similarity": float(row["similarity"])}
        return None


async def fetch_active_clusters() -> list[dict]:
    """Fetch intent clusters that still have uncovered intents and haven't hit the content cap.

    Cap per cluster = min(intent_count, max_content_per_cluster).
    Each intent can produce at most one article, hard-capped at the configured max.
    """
    hard_max = settings.max_content_per_cluster
    async with get_session() as session:
        result = await session.execute(
            text("""
                SELECT ic.id, ic.name, ic.slug, ic.pillar_intent_id,
                       ic.pillar_content_id, ic.status,
                       ic.intent_count, ic.covered_count,
                       ic.priority_score, ic.role_id,
                       COUNT(c.id) AS content_count
                FROM intent_clusters ic
                LEFT JOIN content c ON c.cluster = ic.slug
                WHERE ic.status IN ('active', 'expanding')
                  AND ic.covered_count < ic.intent_count
                GROUP BY ic.id
                HAVING COUNT(c.id) < LEAST(ic.intent_count, :hard_max)
                ORDER BY ic.priority_score DESC
            """),
            {"hard_max": hard_max},
        )
        return [dict(r) for r in result.mappings().all()]


async def fetch_cluster_intents(
    cluster_id: int,
    status: str = "pending",
) -> list[dict]:
    """Fetch intents for a cluster, optionally filtered by status."""
    async with get_session() as session:
        result = await session.execute(
            text("""
                SELECT id, title, embedding IS NOT NULL AS has_embedding,
                       source, volume_hint, priority_score, is_pillar, status
                FROM intents
                WHERE cluster_id = :cid AND status = :status
                ORDER BY is_pillar DESC, priority_score DESC
            """),
            {"cid": cluster_id, "status": status},
        )
        return [dict(r) for r in result.mappings().all()]


async def mark_intent_covered(intent_id: int, content_id: str) -> None:
    """Mark an intent as covered and link it to the produced content."""
    async with get_session() as session:
        await session.execute(
            update(IntentRow)
            .where(IntentRow.id == intent_id)
            .values(status="covered", content_id=content_id, covered_at=func.now())
        )
        # Increment cluster's covered_count
        await session.execute(
            text("""
                UPDATE intent_clusters
                SET covered_count = covered_count + 1
                WHERE id = (SELECT cluster_id FROM intents WHERE id = :iid)
            """),
            {"iid": intent_id},
        )
        await session.commit()


async def mark_cluster_covered(cluster_id: int) -> None:
    async with get_session() as session:
        await session.execute(
            update(IntentClusterRow)
            .where(IntentClusterRow.id == cluster_id)
            .values(status="covered")
        )
        await session.commit()


async def fetch_intent_stats() -> dict:
    """Get summary counts for the intent system."""
    async with get_session() as session:
        result = await session.execute(
            text("""
                SELECT
                  COUNT(*) AS total_intents,
                  COUNT(*) FILTER (WHERE status = 'pending') AS pending,
                  COUNT(*) FILTER (WHERE status = 'covered') AS covered,
                  COUNT(DISTINCT cluster_id) AS total_clusters
                FROM intents
            """)
        )
        row = result.mappings().first()
        return dict(row) if row else {}


async def fetch_seed_keywords(
    role_id: int | None = None, enabled_only: bool = False,
) -> list[SeedKeyword]:
    async with get_session() as session:
        stmt = select(SeedKeywordRow)
        if role_id is not None:
            stmt = stmt.where(SeedKeywordRow.role_id == role_id)
        if enabled_only:
            stmt = stmt.where(SeedKeywordRow.enabled.is_(True))
        stmt = stmt.order_by(SeedKeywordRow.created_at.desc())
        result = await session.execute(stmt)
        return [SeedKeyword.model_validate(r) for r in result.scalars().all()]


async def insert_seed_keyword(
    role_id: int, keyword: str, source: str = "manual", score: float | None = None,
) -> None:
    async with get_session() as session:
        # Insert if missing; if it exists, refresh the score so the latest trends value wins
        stmt = (
            pg_insert(SeedKeywordRow)
            .values(role_id=role_id, keyword=keyword, source=source, score=score, enabled=True)
        )
        if score is not None:
            stmt = stmt.on_conflict_do_update(
                index_elements=["role_id", "keyword"],
                set_={"score": score},
            )
        else:
            stmt = stmt.on_conflict_do_nothing(index_elements=["role_id", "keyword"])
        await session.execute(stmt)
        await session.commit()


async def toggle_seed_keyword(keyword_id: int) -> None:
    async with get_session() as session:
        await session.execute(
            text("UPDATE seed_keywords SET enabled = NOT enabled WHERE id = :id"),
            {"id": keyword_id},
        )
        await session.commit()


async def delete_seed_keyword(keyword_id: int) -> None:
    async with get_session() as session:
        await session.execute(
            text("DELETE FROM seed_keywords WHERE id = :id"), {"id": keyword_id}
        )
        await session.commit()


# ── Roles ───────────────────────────────────────────────────────

async def fetch_roles(enabled_only: bool = False) -> list[Role]:
    async with get_session() as session:
        stmt = select(RoleRow)
        if enabled_only:
            stmt = stmt.where(RoleRow.enabled.is_(True))
        stmt = stmt.order_by(RoleRow.created_at.asc())
        result = await session.execute(stmt)
        return [Role.model_validate(r) for r in result.scalars().all()]


async def fetch_role(role_id: int) -> Role | None:
    async with get_session() as session:
        result = await session.execute(select(RoleRow).where(RoleRow.id == role_id))
        row = result.scalar_one_or_none()
        return Role.model_validate(row) if row else None


async def insert_role(name: str, description: str = "") -> int:
    """Insert a role; slug is auto-derived from the name."""
    import re

    base = re.sub(r"[^\w\s-]", "", name.lower()).strip()
    base = re.sub(r"[\s_]+", "-", base) or "role"
    async with get_session() as session:
        # Make slug unique by appending -2, -3 if collisions exist
        slug = base
        n = 2
        while True:
            existing = await session.execute(
                text("SELECT 1 FROM roles WHERE slug = :s LIMIT 1"), {"s": slug}
            )
            if existing.first() is None:
                break
            slug = f"{base}-{n}"
            n += 1

        result = await session.execute(
            insert(RoleRow)
            .values(slug=slug, name=name, description=description, enabled=True)
            .returning(RoleRow.id)
        )
        rid = result.scalar_one()
        await session.commit()
        return rid


async def update_role(role_id: int, **fields: Any) -> None:
    if not fields:
        return
    async with get_session() as session:
        await session.execute(update(RoleRow).where(RoleRow.id == role_id).values(**fields))
        await session.commit()


async def delete_role(role_id: int) -> None:
    async with get_session() as session:
        await session.execute(text("DELETE FROM roles WHERE id = :id"), {"id": role_id})
        await session.commit()


# ── Role Social Accounts ────────────────────────────────────────

async def fetch_role_accounts(
    role_id: int, enabled_only: bool = False,
) -> list[RoleSocialAccount]:
    async with get_session() as session:
        stmt = select(RoleSocialAccountRow).where(RoleSocialAccountRow.role_id == role_id)
        if enabled_only:
            stmt = stmt.where(RoleSocialAccountRow.enabled.is_(True))
        stmt = stmt.order_by(RoleSocialAccountRow.platform, RoleSocialAccountRow.display_name)
        result = await session.execute(stmt)
        return [RoleSocialAccount.model_validate(r) for r in result.scalars().all()]


async def insert_role_account(
    role_id: int, platform: str, display_name: str, credentials: dict
) -> None:
    async with get_session() as session:
        stmt = (
            pg_insert(RoleSocialAccountRow)
            .values(
                role_id=role_id,
                platform=platform,
                display_name=display_name,
                credentials=credentials,
                enabled=True,
            )
            .on_conflict_do_update(
                index_elements=["role_id", "platform", "display_name"],
                set_={"credentials": credentials, "enabled": True},
            )
        )
        await session.execute(stmt)
        await session.commit()


async def update_role_account(account_id: int, **fields: Any) -> None:
    if not fields:
        return
    async with get_session() as session:
        await session.execute(
            update(RoleSocialAccountRow).where(RoleSocialAccountRow.id == account_id).values(**fields)
        )
        await session.commit()


async def toggle_role_account(account_id: int) -> None:
    async with get_session() as session:
        await session.execute(
            text("UPDATE role_social_accounts SET enabled = NOT enabled WHERE id = :id"),
            {"id": account_id},
        )
        await session.commit()


async def delete_role_account(account_id: int) -> None:
    async with get_session() as session:
        await session.execute(
            text("DELETE FROM role_social_accounts WHERE id = :id"), {"id": account_id}
        )
        await session.commit()


# ── Prompts ─────────────────────────────────────────────────────

async def fetch_prompts() -> list[Prompt]:
    async with get_session() as session:
        result = await session.execute(select(PromptRow).order_by(PromptRow.key))
        return [Prompt.model_validate(r) for r in result.scalars().all()]


async def fetch_prompt_by_key(key: str) -> Prompt | None:
    async with get_session() as session:
        result = await session.execute(select(PromptRow).where(PromptRow.key == key))
        row = result.scalar_one_or_none()
        return Prompt.model_validate(row) if row else None


async def upsert_prompt(key: str, name: str, body: str, description: str = "") -> None:
    """Insert or update a prompt by key."""
    async with get_session() as session:
        stmt = (
            pg_insert(PromptRow)
            .values(key=key, name=name, body=body, description=description)
            .on_conflict_do_update(
                index_elements=["key"],
                set_={"name": name, "body": body, "description": description,
                      "updated_at": func.now()},
            )
        )
        await session.execute(stmt)
        await session.commit()


async def update_prompt_body(prompt_id: int, body: str) -> None:
    async with get_session() as session:
        await session.execute(
            update(PromptRow).where(PromptRow.id == prompt_id)
            .values(body=body, updated_at=func.now())
        )
        await session.commit()


# ── Settings ────────────────────────────────────────────────────

async def fetch_settings() -> list[Setting]:
    async with get_session() as session:
        result = await session.execute(select(SettingRow).order_by(SettingRow.key))
        return [Setting.model_validate(r) for r in result.scalars().all()]


async def fetch_setting(key: str) -> Setting | None:
    async with get_session() as session:
        result = await session.execute(select(SettingRow).where(SettingRow.key == key))
        row = result.scalar_one_or_none()
        return Setting.model_validate(row) if row else None


async def upsert_setting(key: str, value: str, description: str = "") -> None:
    async with get_session() as session:
        stmt = (
            pg_insert(SettingRow)
            .values(key=key, value=value, description=description)
            .on_conflict_do_update(
                index_elements=["key"],
                set_={"value": value, "description": description,
                      "updated_at": func.now()},
            )
        )
        await session.execute(stmt)
        await session.commit()


async def update_setting_value(setting_id: int, value: str) -> None:
    async with get_session() as session:
        await session.execute(
            update(SettingRow).where(SettingRow.id == setting_id)
            .values(value=value, updated_at=func.now())
        )
        await session.commit()


async def delete_setting(setting_id: int) -> None:
    async with get_session() as session:
        await session.execute(text("DELETE FROM settings WHERE id = :id"), {"id": setting_id})
        await session.commit()


# ── Users ───────────────────────────────────────────────────────

async def fetch_users() -> list[User]:
    async with get_session() as session:
        result = await session.execute(select(UserRow).order_by(UserRow.created_at.asc()))
        return [User.model_validate(r) for r in result.scalars().all()]


async def fetch_user(user_id: int) -> User | None:
    async with get_session() as session:
        result = await session.execute(select(UserRow).where(UserRow.id == user_id))
        row = result.scalar_one_or_none()
        return User.model_validate(row) if row else None


async def fetch_user_by_email(email: str) -> User | None:
    async with get_session() as session:
        result = await session.execute(select(UserRow).where(UserRow.email == email))
        row = result.scalar_one_or_none()
        return User.model_validate(row) if row else None


async def insert_user(email: str, hashed_password: str, role: str = "editor") -> int:
    async with get_session() as session:
        result = await session.execute(
            insert(UserRow)
            .values(email=email, hashed_password=hashed_password, role=role)
            .returning(UserRow.id)
        )
        uid = result.scalar_one()
        await session.commit()
        return uid


async def update_user(user_id: int, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = func.now()
    async with get_session() as session:
        await session.execute(
            update(UserRow).where(UserRow.id == user_id).values(**fields)
        )
        await session.commit()


async def delete_user(user_id: int) -> None:
    async with get_session() as session:
        await session.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})
        await session.commit()


async def ping() -> bool:
    try:
        async with get_session() as session:
            await session.execute(text("SELECT 1"))
        return True
    except Exception:
        return False

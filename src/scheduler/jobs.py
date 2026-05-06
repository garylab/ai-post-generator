from __future__ import annotations

import asyncio
import json
import uuid

from src.approval.telegram_bot import send_for_approval
from src.config import settings
from src.content.featured_image import generate_featured
from src.content.generator import generate
from src.content.humanizer import humanize
from src.content.image_enricher import enrich
from src.content.researcher import research_topic
from src.content.wechat_converter import convert_to_wechat
from src.feedback.ab_analyzer import analyze_ab_results, get_preferred_variant
from src.feedback.content_iterator import iterate_low_ctr
from src.feedback.dashboard_export import export_dashboard
from src.feedback.metrics_collector import collect_and_compute
from src.publishers.base import PublishResult
from src.publishers.facebook import FacebookPublisher
from src.publishers.linkedin import LinkedInPublisher
from src.publishers.medium import MediumPublisher
from src.publishers.wechat import WechatPublisher
from src.publishers.website import WebsitePublisher
from src.storage import database as db
from src.storage.models import ContentPackage, ScoredTopic
from src.utils.ai_client import embed_text
from loguru import logger as log


PUBLISHER_REGISTRY = {
    "website": WebsitePublisher,
    "medium": MediumPublisher,
    "linkedin": LinkedInPublisher,
    "facebook": FacebookPublisher,
    "wechat": WechatPublisher,
}


async def _build_publishers_for_role(role: dict | None) -> list:
    """Construct publisher instances from a role's enabled social accounts."""
    if not role:
        return []
    accounts = await db.fetch_role_accounts(role["id"], enabled_only=True)
    publishers = []
    for acc in accounts:
        cls = PUBLISHER_REGISTRY.get(acc["platform"])
        if not cls:
            log.warning("Unknown platform '{}' for role '{}'", acc["platform"], role["slug"])
            continue
        creds = dict(acc.get("credentials") or {})
        publishers.append(cls(credentials=creds, display_name=acc.get("display_name", "")))
    # WeChat publisher is currently a stub but always runs to log content readiness
    publishers.append(WechatPublisher())
    return publishers


# ── Stage 1: Research ─────────────────────────────────────────

async def stage_research() -> int:
    """Pick pending intents, research them, persist research data.

    Reads: intents (pending) via intent_clusters
    Writes: content rows with status='researched' + research_data JSON
    Returns: number of intents researched
    """
    log.info("[stage_research] Looking for pending intents...")

    active_clusters = await db.fetch_active_clusters()
    if not active_clusters:
        log.info("[stage_research] No active clusters with pending intents")
        return 0

    cap = settings.max_articles_per_run
    intents_to_write: list[tuple[dict, dict]] = []

    for cluster in active_clusters:
        if len(intents_to_write) >= cap:
            break
        pending = await db.fetch_cluster_intents(cluster["id"], status="pending")
        if not pending:
            await db.mark_cluster_covered(cluster["id"])
            log.info("[stage_research] Cluster '{}' fully covered", cluster["name"])
            continue
        for intent in pending:
            if len(intents_to_write) >= cap:
                break
            intents_to_write.append((intent, cluster))

    if not intents_to_write:
        log.info("[stage_research] No pending intents to research")
        return 0

    log.info("[stage_research] Researching {} intents (cap={})", len(intents_to_write), cap)
    count = 0

    for intent, cluster in intents_to_write:
        try:
            title_emb = await embed_text(intent["title"])
            if title_emb:
                existing = await db.find_similar_content(title_emb, threshold=0.85, days=60)
                if existing:
                    log.warning("[stage_research] Skipping '{}' — similar to '{}' (sim={:.3f})",
                                intent["title"], existing["title"], existing["similarity"])
                    await db.mark_intent_covered(intent["id"], existing["content_id"])
                    continue

            research = await research_topic(intent["title"])

            content_id = f"mr-{uuid.uuid4().hex[:12]}"
            raw_score = float(intent.get("priority_score", 7))
            content_score = round(min(raw_score / 125, 10.0), 1)

            research_payload = {
                "synthesis": research.get("synthesis", ""),
                "sources": research.get("sources", []),
                "source_images": research.get("source_images", []),
            }

            await db.insert_researched_content(
                content_id=content_id,
                title=intent["title"],
                cluster=cluster["slug"],
                score=content_score,
                intent_id=intent["id"],
                research_data=research_payload,
                title_embedding=title_emb,
                role_id=cluster.get("role_id"),
            )

            await db.mark_intent_covered(intent["id"], content_id)

            pillar_tag = " [PILLAR]" if intent.get("is_pillar") else ""
            log.info("[stage_research] Researched '{}' → {} (cluster: {}){}",
                     intent["title"], content_id, cluster["name"], pillar_tag)
            count += 1

        except Exception as exc:
            log.error("[stage_research] Failed '{}': {}", intent["title"], exc, exc_info=True)

    log.info("[stage_research] Done — {} intents researched", count)
    return count


# ── Stage 2: Generate ─────────────────────────────────────────

async def stage_generate() -> int:
    """Generate article HTML + social posts from researched content.

    Reads: content WHERE status='researched'
    Writes: article_html, social_posts, outline, etc. → status='generated'
    """
    rows = await db.fetch_content_by_status("researched", limit=settings.max_articles_per_run)
    if not rows:
        log.info("[stage_generate] No 'researched' content to generate")
        return 0

    log.info("[stage_generate] Generating {} articles...", len(rows))
    count = 0

    for row in rows:
        try:
            rd = row.get("research_data") or {}
            if isinstance(rd, str):
                rd = json.loads(rd)

            source_urls = [s["url"] for s in rd.get("sources", []) if s.get("url")]

            topic = ScoredTopic(
                title=row["title"],
                source="intent",
                score=float(row.get("score", 7)),
                decision="WRITE",
                suggested_angle=row.get("suggested_angle", "") or "",
                cluster=row.get("cluster", "other") or "other",
                source_urls=source_urls,
            )

            research = {
                "synthesis": rd.get("synthesis", ""),
                "sources": rd.get("sources", []),
            }

            pkg = await generate(topic, research=research)
            pkg.source_images = rd.get("source_images", [])
            pkg = await humanize(pkg)

            await db.update_content_stage(
                row["content_id"],
                "generated",
                article_html=pkg.article_html,
                medium_article=pkg.medium_article,
                outline=pkg.outline,
                social_posts=pkg.social_posts,
                social_posts_variant_b=pkg.social_posts_variant_b,
                seo_keywords=pkg.seo_keywords,
                meta_description=pkg.meta_description,
                cta_variant_a=pkg.cta_variant_a,
                cta_variant_b=pkg.cta_variant_b,
                title=pkg.article_title,
            )

            log.info("[stage_generate] Generated '{}'", pkg.article_title)
            count += 1

        except Exception as exc:
            log.error("[stage_generate] Failed '{}': {}", row.get("title", "?"), exc, exc_info=True)

    log.info("[stage_generate] Done — {} articles generated", count)
    return count


# ── Stage 3: Enrich ───────────────────────────────────────────

async def stage_enrich() -> int:
    """Enrich generated articles with images, featured image, WeChat conversion.

    Reads: content WHERE status='generated'
    Writes: image_url, wechat_article, enriched HTML → status='enriched'
    """
    rows = await db.fetch_content_by_status("generated", limit=settings.max_articles_per_run)
    if not rows:
        log.info("[stage_enrich] No 'generated' content to enrich")
        return 0

    log.info("[stage_enrich] Enriching {} articles...", len(rows))
    count = 0

    for row in rows:
        try:
            rd = row.get("research_data") or {}
            if isinstance(rd, str):
                rd = json.loads(rd)

            pkg = _row_to_package(row)
            pkg.source_images = rd.get("source_images", [])

            pkg = await enrich(pkg)
            pkg = await generate_featured(pkg)
            pkg = await convert_to_wechat(pkg)

            await db.update_content_stage(
                row["content_id"],
                "enriched",
                article_html=pkg.article_html,
                image_url=pkg.featured_image_url,
                wechat_article=pkg.wechat_article or None,
            )

            log.info("[stage_enrich] Enriched '{}'", row["title"])
            count += 1

        except Exception as exc:
            log.error("[stage_enrich] Failed '{}': {}", row.get("title", "?"), exc, exc_info=True)

    log.info("[stage_enrich] Done — {} articles enriched", count)
    return count


# ── Stage 4: Finalize ─────────────────────────────────────────

async def stage_finalize() -> int:
    """Final pass: embed title, mark as draft, approve/publish.

    Reads: content WHERE status='enriched'
    Writes: title_embedding → status='draft', then auto-approve or Telegram
    """
    rows = await db.fetch_content_by_status("enriched", limit=settings.max_articles_per_run)
    if not rows:
        log.info("[stage_finalize] No 'enriched' content to finalize")
        return 0

    log.info("[stage_finalize] Finalizing {} articles...", len(rows))
    count = 0

    for row in rows:
        try:
            final_emb = await embed_text(row["title"])

            await db.update_content_stage(
                row["content_id"],
                "draft",
                title_embedding=final_emb,
            )

            if settings.auto_approve:
                await db.update_content_status(row["content_id"], "approved")
                log.info("[stage_finalize] Auto-approved: '{}'", row["title"])
                await publish_approved(row["content_id"])
            else:
                pkg = _row_to_package(row)
                await send_for_approval(pkg)
                log.info("[stage_finalize] Queued for approval: '{}'", row["title"])

            count += 1

        except Exception as exc:
            log.error("[stage_finalize] Failed '{}': {}", row.get("title", "?"), exc, exc_info=True)

    log.info("[stage_finalize] Done — {} articles finalized", count)
    return count


# ── Orchestrator ──────────────────────────────────────────────

async def main_pipeline() -> None:
    """Run all production stages in sequence.

    Each stage independently reads from DB and writes back,
    so a crash between stages loses no work.
    """
    log.info("========== Starting intent-driven pipeline ==========")
    try:
        r = await stage_research()
        g = await stage_generate()
        e = await stage_enrich()
        f = await stage_finalize()

        stats = await db.fetch_intent_stats()
        log.info(
            "========== Pipeline complete: researched={}, generated={}, enriched={}, finalized={} "
            "(DB: {} pending, {} covered) ==========",
            r, g, e, f,
            stats.get("pending", 0), stats.get("covered", 0),
        )
    except Exception as exc:
        log.error("Pipeline failed: {}", exc, exc_info=True)


# ── Helpers ───────────────────────────────────────────────────

def _row_to_package(row: dict) -> ContentPackage:
    """Reconstruct a ContentPackage from a content DB row."""
    social = row.get("social_posts", "{}")
    if isinstance(social, str):
        try:
            social = json.loads(social)
        except json.JSONDecodeError:
            social = {}

    social_b = row.get("social_posts_variant_b", "{}")
    if isinstance(social_b, str):
        try:
            social_b = json.loads(social_b)
        except json.JSONDecodeError:
            social_b = {}

    keywords = row.get("seo_keywords", "[]")
    if isinstance(keywords, str):
        try:
            keywords = json.loads(keywords)
        except json.JSONDecodeError:
            keywords = []

    outline = row.get("outline", "[]")
    if isinstance(outline, str):
        try:
            outline = json.loads(outline)
        except json.JSONDecodeError:
            outline = []

    return ContentPackage(
        content_id=row.get("content_id", ""),
        article_title=row.get("title", ""),
        article_html=row.get("article_html", "") or "",
        medium_article=row.get("medium_article", "") or "",
        wechat_article=row.get("wechat_article", "") or "",
        social_posts=social if isinstance(social, dict) else {},
        social_posts_variant_b=social_b if isinstance(social_b, dict) else {},
        seo_keywords=keywords if isinstance(keywords, list) else [],
        meta_description=row.get("meta_description", "") or "",
        cta_variant_a=row.get("cta_variant_a", "") or "",
        cta_variant_b=row.get("cta_variant_b", "") or "",
        featured_image_url=row.get("image_url", "") or "",
        outline=outline if isinstance(outline, list) else [],
    )


# ── Publishing ────────────────────────────────────────────────

async def publish_approved(content_id: str) -> None:
    """Called when content is approved — publish to the owning role's accounts."""
    log.info("Publishing approved content: {}", content_id)
    row = await db.fetch_content(content_id)
    if not row:
        log.warning("Content {} not found", content_id)
        return

    role = await db.fetch_role(row["role_id"]) if row.get("role_id") else None
    if not role:
        log.warning("Content {} has no associated role — cannot publish", content_id)
        return

    publishers = await _build_publishers_for_role(role)
    if not publishers:
        log.warning("Role '{}' has no enabled social accounts", role["slug"])
        return

    pkg = _row_to_package(row)
    cta_variant = await get_preferred_variant()
    log.info("Using CTA variant '{}' (A/B winner) for role '{}'", cta_variant, role["slug"])

    results: list[PublishResult | Exception] = await asyncio.gather(
        *[p.publish(pkg, cta_variant) for p in publishers],
        return_exceptions=True,
    )

    published = 0
    for r in results:
        if isinstance(r, Exception):
            log.error("Publish error: {}", r)
            continue
        if r.success:
            await db.insert_publish_log(
                content_id, r.platform, r.url, cta_variant, r.post_body,
            )
            published += 1
        else:
            log.warning("Publish failed on {}: {}", r.platform, r.error)

    await db.update_content_status(content_id, "published")
    log.info("Published {} to {}/{} platforms", content_id, published, len(publishers))


# ── Growth Loop ───────────────────────────────────────────────

async def growth_loop() -> None:
    """Expand covered clusters with deeper intents."""
    from src.pipeline.intent_miner import mine_intents
    from src.pipeline.intent_clusterer import process_intents

    log.info("========== Starting growth loop ==========")
    try:
        hard_max = settings.max_content_per_cluster
        async with db.get_session() as session:
            result = await session.execute(
                db.text("""
                    SELECT ic.id, ic.name, ic.slug, ic.pillar_intent_id,
                           COUNT(c.id) AS content_count
                    FROM intent_clusters ic
                    LEFT JOIN content c ON c.cluster = ic.slug
                    WHERE ic.status = 'active'
                      AND ic.covered_count >= ic.intent_count
                      AND ic.intent_count > 0
                    GROUP BY ic.id
                    HAVING COUNT(c.id) < LEAST(ic.intent_count, :hard_max)
                    ORDER BY ic.priority_score DESC
                    LIMIT 10
                """),
                {"hard_max": hard_max},
            )
            covered_clusters = [dict(r) for r in result.mappings().all()]

        if not covered_clusters:
            log.info("No fully-covered clusters to expand")
            return

        log.info("Found {} fully-covered clusters to expand", len(covered_clusters))

        for cluster in covered_clusters:
            pillar_id = cluster.get("pillar_intent_id")
            if not pillar_id:
                continue

            async with db.get_session() as session:
                result = await session.execute(
                    db.text("SELECT title FROM intents WHERE id = :id"),
                    {"id": pillar_id},
                )
                row = result.mappings().first()

            if not row:
                continue

            seed = row["title"]
            log.info("Expanding cluster '{}' with seed '{}'", cluster["name"], seed)

            raw_intents = await mine_intents([seed])
            if raw_intents:
                batch_id = str(uuid.uuid4())
                summary = await process_intents(raw_intents, batch_id)
                log.info("Added {} new intents from expansion of '{}'",
                         summary.get("intents", 0), cluster["name"])

            await db.mark_cluster_covered(cluster["id"])

        log.info("========== Growth loop complete ==========")
    except Exception as exc:
        log.error("Growth loop failed: {}", exc, exc_info=True)


# ── Per-cluster pipeline (manual trigger) ─────────────────────

async def run_pipeline_for_cluster(cluster_id: int, max_articles: int | None = None) -> None:
    """Research + generate + enrich + finalize all pending intents in one cluster."""
    from src.content.researcher import research_topic

    log.info("========== Starting pipeline for cluster id={} ==========", cluster_id)
    try:
        cluster_rows = await db._fetch_clusters_by_id([cluster_id]) if hasattr(db, "_fetch_clusters_by_id") else None
        # Fallback: query directly
        cluster = None
        if not cluster_rows:
            from sqlalchemy import text as _t
            async with db.get_session() as s:
                row = (await s.execute(_t(
                    "SELECT id, name, slug, role_id FROM intent_clusters WHERE id = :id"
                ), {"id": cluster_id})).mappings().first()
                if row:
                    cluster = dict(row)
        else:
            cluster = cluster_rows[0]

        if not cluster:
            log.warning("Cluster id={} not found", cluster_id)
            return

        pending = await db.fetch_cluster_intents(cluster["id"], status="pending")
        if not pending:
            log.info("Cluster '{}' has no pending intents", cluster["name"])
            return

        cap = max_articles or settings.max_articles_per_run
        pending = pending[:cap]
        log.info("Researching {} pending intent(s) in cluster '{}'", len(pending), cluster["name"])

        for intent in pending:
            try:
                title_emb = await embed_text(intent["title"])
                if title_emb:
                    existing = await db.find_similar_content(title_emb, threshold=0.85, days=60)
                    if existing:
                        log.warning("Skipping '{}' — similar to '{}' (sim={:.3f})",
                                    intent["title"], existing["title"], existing["similarity"])
                        await db.mark_intent_covered(intent["id"], existing["content_id"])
                        continue

                research = await research_topic(intent["title"])
                content_id = f"mr-{uuid.uuid4().hex[:12]}"
                raw_score = float(intent.get("priority_score", 7))
                content_score = round(min(raw_score / 125, 10.0), 1)
                research_payload = {
                    "synthesis": research.get("synthesis", ""),
                    "sources": research.get("sources", []),
                    "source_images": research.get("source_images", []),
                }
                await db.insert_researched_content(
                    content_id=content_id,
                    title=intent["title"],
                    cluster=cluster["slug"],
                    score=content_score,
                    intent_id=intent["id"],
                    research_data=research_payload,
                    title_embedding=title_emb,
                    role_id=cluster.get("role_id"),
                )
                await db.mark_intent_covered(intent["id"], content_id)
                log.info("Researched '{}' → {}", intent["title"], content_id)
            except Exception as exc:
                log.error("Research failed for intent {}: {}", intent.get("id"), exc)

        # Continue through downstream stages globally — they'll pick up our new rows
        n_g = await stage_generate()
        n_e = await stage_enrich()
        n_f = await stage_finalize()
        log.info(
            "========== Cluster '{}' pipeline done: generate={} enrich={} finalize={} ==========",
            cluster["name"], n_g, n_e, n_f,
        )
    except Exception as exc:
        log.error("Per-cluster pipeline failed: {}", exc, exc_info=True)


# ── Intent Mining ─────────────────────────────────────────────

async def intent_mining_pipeline(role_id: int | None = None) -> None:
    """Mine user intents per role, deduplicate, cluster, and save to DB.

    If role_id is given, mines only that role; otherwise mines all enabled roles.
    """
    from src.pipeline.intent_miner import mine_intents
    from src.pipeline.intent_clusterer import process_intents

    log.info("========== Starting intent mining pipeline ==========")
    try:
        if role_id is not None:
            role = await db.fetch_role(role_id)
            roles = [role] if role else []
        else:
            roles = await db.fetch_roles(enabled_only=True)
        if not roles:
            log.warning("No roles to mine — skipping")
            return

        for role in roles:
            seeds = [
                k["keyword"]
                for k in await db.fetch_seed_keywords(role_id=role["id"], enabled_only=True)
            ]
            if not seeds:
                log.warning("Role '{}' has no enabled seed keywords — skipping", role["slug"])
                continue

            batch_id = str(uuid.uuid4())
            log.info(
                "[role={}] Mining intents from {} seeds (batch={})",
                role["slug"], len(seeds), batch_id[:8],
            )

            raw_intents = await mine_intents(seeds)
            if not raw_intents:
                log.warning("[role={}] No intents mined", role["slug"])
                continue

            summary = await process_intents(raw_intents, batch_id, role_id=role["id"])
            log.info(
                "[role={}] {} raw → {} new intents in {} clusters",
                role["slug"], summary["total"], summary["intents"], summary["clusters"],
            )

        stats = await db.fetch_intent_stats()
        log.info(
            "========== Intent mining complete (DB total: {} intents, {} pending, {} covered) ==========",
            stats.get("total_intents", 0), stats.get("pending", 0), stats.get("covered", 0),
        )
    except Exception as exc:
        log.error("Intent mining failed: {}", exc, exc_info=True)


# ── Daily Metrics ─────────────────────────────────────────────

async def daily_metrics() -> None:
    """Daily job: collect metrics, A/B analysis, iterate low CTR, export dashboard."""
    log.info("========== Starting daily metrics ==========")
    try:
        metrics = await collect_and_compute(days=7)
        log.info("Computed metrics for {} content-platform pairs", len(metrics))

        ab_result = await analyze_ab_results()
        log.info("A/B analysis: winner={}, confidence={}", ab_result["winner"], ab_result["confidence"])

        regen_count = await iterate_low_ctr(ctr_threshold=1.0, limit=5)
        log.info("Regenerated {} low-CTR articles", regen_count)

        await export_dashboard()

        log.info("========== Daily metrics complete ==========")
    except Exception as exc:
        log.error("Daily metrics failed: {}", exc, exc_info=True)

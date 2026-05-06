from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text

from src.storage.database import (
    delete_brand,
    delete_brand_account,
    delete_seed_keyword,
    delete_setting,
    delete_user,
    fetch_content,
    fetch_prompts,
    fetch_brand,
    fetch_brand_accounts,
    fetch_brands,
    fetch_seed_keywords,
    fetch_settings,
    fetch_user,
    fetch_user_by_email,
    fetch_users,
    get_session,
    insert_brand,
    insert_brand_account,
    insert_seed_keyword,
    insert_user,
    toggle_brand_account,
    toggle_seed_keyword,
    update_prompt_body,
    update_brand,
    update_brand_account,
    update_setting_value,
    update_user,
    upsert_setting,
)
from src.web.auth import (
    current_user,
    hash_password,
    require_admin,
    require_user,
    verify_password,
)

# Public routes (no auth) — login, logout
public_router = APIRouter()

# Protected — every route below requires a logged-in user
router = APIRouter(dependencies=[Depends(require_user)])

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


async def _fetch(sql: str, params: dict | None = None) -> list[dict]:
    async with get_session() as session:
        result = await session.execute(text(sql), params or {})
        return [dict(r) for r in result.mappings().all()]


async def _count(sql: str, params: dict | None = None) -> int:
    async with get_session() as session:
        result = await session.execute(text(sql), params or {})
        return int(result.scalar() or 0)


def _page_params(page: int, per_page: int = 50) -> tuple[int, int, int]:
    """Normalize page (1-based) and return (page, per_page, offset)."""
    page = max(1, page)
    per_page = max(1, min(per_page, 200))
    return page, per_page, (page - 1) * per_page


def _resolve_sort(
    sort: str | None,
    direction: str | None,
    allowed: dict[str, str],
    default_key: str,
    default_dir: str = "desc",
) -> tuple[str, str, str]:
    """Validate sort/direction against whitelist; return (ORDER BY SQL, ui_key, ui_dir)."""
    key = sort if (sort in allowed) else default_key
    d = (direction or "").lower()
    if d not in ("asc", "desc"):
        d = default_dir
    return f"{allowed[key]} {d.upper()}", key, d


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    stats = await _fetch(
        """
        SELECT
          (SELECT COUNT(*) FROM intents) AS total_intents,
          (SELECT COUNT(*) FROM intents WHERE status = 'pending') AS pending_intents,
          (SELECT COUNT(*) FROM intents WHERE status = 'covered') AS covered_intents,
          (SELECT COUNT(*) FROM intent_clusters) AS total_clusters,
          (SELECT COUNT(*) FROM content) AS total_content,
          (SELECT COUNT(*) FROM content WHERE status = 'draft') AS draft_content,
          (SELECT COUNT(*) FROM content WHERE status = 'approved') AS approved_content,
          (SELECT COUNT(*) FROM content WHERE status = 'published') AS published_content,
          (SELECT COUNT(*) FROM publish_logs) AS total_publishes
        """
    )
    by_status = await _fetch(
        "SELECT status::text AS status, COUNT(*) AS n FROM content GROUP BY status ORDER BY n DESC"
    )
    recent_content = await _fetch(
        """
        SELECT content_id, title, cluster, status::text AS status, score, created_at
        FROM content ORDER BY created_at DESC LIMIT 10
        """
    )
    recent_publishes = await _fetch(
        """
        SELECT pl.content_id, c.title, pl.platform::text AS platform,
               pl.published_url, pl.published_at
        FROM publish_logs pl JOIN content c ON pl.content_id = c.content_id
        ORDER BY pl.published_at DESC LIMIT 10
        """
    )
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "stats": stats[0] if stats else {},
            "by_status": by_status,
            "recent_content": recent_content,
            "recent_publishes": recent_publishes,
        },
    )


@router.get("/dashboard/content", response_class=HTMLResponse)
async def content_list(
    request: Request,
    status: str | None = None,
    cluster: str | None = None,
    page: int = 1,
    per_page: int = 50,
    sort: str | None = None,
    dir: str | None = None,
):
    where = []
    params: dict = {}
    if status:
        where.append("c.status::text = :status")
        params["status"] = status
    if cluster:
        where.append("c.cluster = :cluster")
        params["cluster"] = cluster
    clause = ("WHERE " + " AND ".join(where)) if where else ""

    sort_cols = {
        "title": "c.title",
        "cluster": "c.cluster",
        "status": "c.status::text",
        "score": "c.score",
        "iteration_count": "c.iteration_count",
        "created_at": "c.created_at",
        "updated_at": "c.updated_at",
    }
    order_by, sort_key, sort_dir = _resolve_sort(sort, dir, sort_cols, "created_at", "desc")

    page, per_page, offset = _page_params(page, per_page)
    total = await _count(f"SELECT COUNT(*) FROM content c {clause}", params)
    rows = await _fetch(
        f"""
        SELECT c.content_id, c.title, c.cluster, c.status::text AS status, c.score,
               c.iteration_count, c.created_at, c.updated_at,
               ic.id AS cluster_id, ic.name AS cluster_name
        FROM content c LEFT JOIN intent_clusters ic ON c.cluster = ic.slug
        {clause}
        ORDER BY {order_by} LIMIT :lim OFFSET :off
        """,
        {**params, "lim": per_page, "off": offset},
    )
    statuses = await _fetch(
        "SELECT DISTINCT status::text AS s FROM content ORDER BY s"
    )
    clusters = await _fetch(
        "SELECT DISTINCT cluster AS c FROM content WHERE cluster IS NOT NULL ORDER BY c"
    )
    filter_qs = []
    if status: filter_qs.append(f"status={status}")
    if cluster: filter_qs.append(f"cluster={cluster}")
    filter_q = "&".join(filter_qs)
    full_q = "&".join(filter_qs + [f"sort={sort_key}", f"dir={sort_dir}"])
    return templates.TemplateResponse(
        request,
        "content_list.html",
        {
            "rows": rows,
            "statuses": [r["s"] for r in statuses],
            "clusters": [r["c"] for r in clusters],
            "selected_status": status,
            "selected_cluster": cluster,
            "page": page, "per_page": per_page, "total": total,
            "query": full_q,           # used by paginate() so sort survives page changes
            "filter_query": filter_q,  # used by sort_header() (no sort/dir)
            "sort_key": sort_key,
            "sort_dir": sort_dir,
        },
    )


CONTENT_TABS = ("overview", "article", "medium", "wechat", "social", "publishes")


async def _content_or_404(content_id: str) -> dict:
    row = await fetch_content(content_id)
    if not row:
        raise HTTPException(404, "Content not found")
    return row


@router.get("/dashboard/content/{content_id}", response_class=HTMLResponse)
async def content_detail_root(content_id: str):
    return RedirectResponse(f"/dashboard/content/{content_id}/overview", status_code=303)


@router.get("/dashboard/content/{content_id}/{tab}", response_class=HTMLResponse)
async def content_detail_tab(request: Request, content_id: str, tab: str):
    if tab not in CONTENT_TABS:
        raise HTTPException(404, "Unknown tab")
    row = await _content_or_404(content_id)
    ctx = {"row": row, "active_tab": tab}
    if tab == "publishes":
        ctx["publishes"] = await _fetch(
            """
            SELECT platform::text AS platform, published_url, cta_variant::text AS cta_variant,
                   published_at
            FROM publish_logs WHERE content_id = :cid ORDER BY published_at DESC
            """,
            {"cid": content_id},
        )
    return templates.TemplateResponse(request, "content_detail.html", ctx)


@router.get("/dashboard/intents", response_class=HTMLResponse)
async def intents_list(
    request: Request,
    status: str | None = None,
    page: int = 1,
    per_page: int = 50,
    sort: str | None = None,
    dir: str | None = None,
):
    where = ""
    params: dict = {}
    if status:
        where = "WHERE i.status::text = :status"
        params["status"] = status

    sort_cols = {
        "title": "i.title",
        "source": "i.source",
        "cluster_slug": "ic.slug",
        "status": "i.status::text",
        "priority_score": "i.priority_score",
        "is_pillar": "i.is_pillar",
        "created_at": "i.created_at",
    }
    order_by, sort_key, sort_dir = _resolve_sort(sort, dir, sort_cols, "created_at", "desc")

    page, per_page, offset = _page_params(page, per_page)
    total = await _count(
        f"SELECT COUNT(*) FROM intents i LEFT JOIN intent_clusters ic ON i.cluster_id = ic.id {where}",
        params,
    )
    rows = await _fetch(
        f"""
        SELECT i.id, i.title, i.source, i.source_url, i.status::text AS status,
               i.priority_score, i.is_pillar, i.created_at,
               i.cluster_id, ic.slug AS cluster_slug, ic.name AS cluster_name
        FROM intents i LEFT JOIN intent_clusters ic ON i.cluster_id = ic.id
        {where}
        ORDER BY {order_by} LIMIT :lim OFFSET :off
        """,
        {**params, "lim": per_page, "off": offset},
    )
    statuses = await _fetch("SELECT DISTINCT status::text AS s FROM intents ORDER BY s")
    filter_q = f"status={status}" if status else ""
    full_q = "&".join(([filter_q] if filter_q else []) + [f"sort={sort_key}", f"dir={sort_dir}"])
    return templates.TemplateResponse(
        request,
        "intents.html",
        {
            "rows": rows,
            "statuses": [r["s"] for r in statuses],
            "selected_status": status,
            "page": page, "per_page": per_page, "total": total,
            "query": full_q,
            "filter_query": filter_q,
            "sort_key": sort_key,
            "sort_dir": sort_dir,
        },
    )


@router.get("/dashboard/clusters/{cluster_id}/intents.json")
async def cluster_intents_json(cluster_id: int):
    rows = await _fetch(
        """
        SELECT i.id, i.title, i.source, i.status::text AS status,
               i.priority_score, i.is_pillar, i.created_at,
               CASE
                 WHEN i.embedding IS NOT NULL AND ic.centroid_embedding IS NOT NULL
                 THEN 1 - (i.embedding <=> ic.centroid_embedding)
                 ELSE NULL
               END AS similarity
        FROM intents i
        JOIN intent_clusters ic ON ic.id = i.cluster_id
        WHERE i.cluster_id = :cid
        ORDER BY similarity DESC NULLS LAST, i.is_pillar DESC, i.priority_score DESC
        """,
        {"cid": cluster_id},
    )
    for r in rows:
        if r.get("created_at"):
            r["created_at"] = r["created_at"].isoformat()
        if r.get("priority_score") is not None:
            r["priority_score"] = float(r["priority_score"])
        if r.get("similarity") is not None:
            r["similarity"] = float(r["similarity"])
    return rows


@router.post("/dashboard/clusters/{cluster_id}/generate")
async def cluster_generate(cluster_id: int):
    """Synchronously enqueue content rows, then run the background pipeline."""
    from src.scheduler.jobs import enqueue_cluster, run_pipeline_for_cluster
    result = await enqueue_cluster(cluster_id)
    if result["queued"] > 0:
        asyncio.create_task(run_pipeline_for_cluster(cluster_id))
    return RedirectResponse("/dashboard/content?status=queued", status_code=303)


@router.get("/dashboard/clusters", response_class=HTMLResponse)
async def clusters_list(
    request: Request,
    page: int = 1,
    per_page: int = 50,
    sort: str | None = None,
    dir: str | None = None,
):
    sort_cols = {
        "name": "name",
        "slug": "slug",
        "status": "status::text",
        "intent_count": "intent_count",
        "covered_count": "covered_count",
        "priority_score": "priority_score",
        "created_at": "created_at",
    }
    order_by, sort_key, sort_dir = _resolve_sort(sort, dir, sort_cols, "priority_score", "desc")

    page, per_page, offset = _page_params(page, per_page)
    total = await _count("SELECT COUNT(*) FROM intent_clusters")
    rows = await _fetch(
        f"""
        SELECT id, name, slug, status::text AS status,
               intent_count, covered_count, priority_score, created_at
        FROM intent_clusters
        ORDER BY {order_by} LIMIT :lim OFFSET :off
        """,
        {"lim": per_page, "off": offset},
    )
    full_q = f"sort={sort_key}&dir={sort_dir}"
    return templates.TemplateResponse(
        request, "clusters.html",
        {
            "rows": rows, "page": page, "per_page": per_page, "total": total,
            "query": full_q, "filter_query": "",
            "sort_key": sort_key, "sort_dir": sort_dir,
        },
    )


# ── Roles ──────────────────────────────────────────────────────

# Schema for credentials per platform — used to render dynamic forms
PLATFORM_FIELDS = {
    "website": [("api_url", "API URL"), ("api_key", "API Key")],
    "medium": [("api_token", "API Token"), ("author_id", "Author ID")],
    "linkedin": [("access_token", "Access Token"), ("person_urn", "Person URN")],
    "facebook": [("page_id", "Page ID"), ("access_token", "Access Token")],
    "wechat": [("app_id", "App ID"), ("app_secret", "App Secret")],
}


@router.get("/dashboard/brands", response_class=HTMLResponse)
async def brands_list(
    request: Request,
    page: int = 1,
    per_page: int = 50,
    sort: str | None = None,
    dir: str | None = None,
):
    sort_cols = {
        "name": "name",
        "enabled": "enabled",
        "created_at": "created_at",
    }
    order_by, sort_key, sort_dir = _resolve_sort(sort, dir, sort_cols, "created_at", "asc")

    page, per_page, offset = _page_params(page, per_page)
    total = await _count("SELECT COUNT(*) FROM brands")
    rows = await _fetch(
        f"""
        SELECT id, slug, name, description, enabled, created_at
        FROM brands ORDER BY {order_by} LIMIT :lim OFFSET :off
        """,
        {"lim": per_page, "off": offset},
    )
    counts = await _fetch(
        """
        SELECT r.id,
               (SELECT COUNT(*) FROM seed_keywords WHERE brand_id = r.id) AS keyword_count,
               (SELECT COUNT(*) FROM brand_social_accounts WHERE brand_id = r.id) AS account_count,
               (SELECT COUNT(*) FROM content WHERE brand_id = r.id) AS content_count
        FROM brands r
        """
    )
    counts_by_id = {c["id"]: c for c in counts}
    full_q = f"sort={sort_key}&dir={sort_dir}"
    return templates.TemplateResponse(
        request, "brands.html",
        {
            "rows": rows, "counts": counts_by_id,
            "page": page, "per_page": per_page, "total": total,
            "query": full_q, "filter_query": "",
            "sort_key": sort_key, "sort_dir": sort_dir,
        },
    )


@router.post("/dashboard/brands/add")
async def brands_add(name: str = Form(...), description: str = Form("")):
    rid = await insert_brand(name.strip(), description.strip())
    return RedirectResponse(f"/dashboard/brands/{rid}", status_code=303)


@router.get("/dashboard/brands/{brand_id}", response_class=HTMLResponse)
async def brand_detail_root(brand_id: int):
    return RedirectResponse(f"/dashboard/brands/{brand_id}/keywords", status_code=303)


@router.get("/dashboard/brands/{brand_id}/overall", response_class=HTMLResponse)
async def brand_detail_overall_legacy(brand_id: int):
    # Legacy alias — overall was renamed to settings.
    return RedirectResponse(f"/dashboard/brands/{brand_id}/settings", status_code=303)


async def _brand_or_404(brand_id: int) -> dict:
    brand = await fetch_brand(brand_id)
    if not brand:
        raise HTTPException(404, "Brand not found")
    return brand


@router.get("/dashboard/brands/{brand_id}/settings", response_class=HTMLResponse)
async def brand_tab_settings(request: Request, brand_id: int):
    brand = await _brand_or_404(brand_id)
    return templates.TemplateResponse(
        request, "brand_detail.html",
        {"brand": brand, "active_tab": "settings"},
    )


@router.get("/dashboard/brands/{brand_id}/social-accounts", response_class=HTMLResponse)
async def brand_tab_accounts(request: Request, brand_id: int):
    brand = await _brand_or_404(brand_id)
    accounts = await fetch_brand_accounts(brand_id)
    return templates.TemplateResponse(
        request, "brand_detail.html",
        {
            "brand": brand,
            "active_tab": "social-accounts",
            "accounts": accounts,
            "platform_fields": PLATFORM_FIELDS,
        },
    )


@router.get("/dashboard/brands/{brand_id}/keywords", response_class=HTMLResponse)
async def brand_tab_keywords(request: Request, brand_id: int):
    brand = await _brand_or_404(brand_id)
    keywords = await fetch_seed_keywords(brand_id=brand_id)
    return templates.TemplateResponse(
        request, "brand_detail.html",
        {"brand": brand, "active_tab": "keywords", "keywords": keywords},
    )


@router.get("/dashboard/brands/{brand_id}/notifications", response_class=HTMLResponse)
async def brand_tab_notifications(request: Request, brand_id: int):
    brand = await _brand_or_404(brand_id)
    return templates.TemplateResponse(
        request, "brand_detail.html",
        {"brand": brand, "active_tab": "notifications"},
    )


@router.post("/dashboard/brands/{brand_id}/update")
async def brands_update(
    brand_id: int,
    name: str = Form(...),
    description: str = Form(""),
    enabled: str = Form(None),
):
    await update_brand(
        brand_id,
        name=name.strip(),
        description=description.strip(),
        enabled=bool(enabled),
    )
    return RedirectResponse(f"/dashboard/brands/{brand_id}/settings", status_code=303)


@router.post("/dashboard/brands/{brand_id}/notifications/update")
async def brands_notifications_update(
    brand_id: int,
    telegram_bot_token: str = Form(""),
    telegram_chat_id: str = Form(""),
    telegram_enabled: str = Form(None),
):
    await update_brand(
        brand_id,
        telegram_bot_token=telegram_bot_token.strip(),
        telegram_chat_id=telegram_chat_id.strip(),
        telegram_enabled=bool(telegram_enabled),
    )
    return RedirectResponse(f"/dashboard/brands/{brand_id}/notifications", status_code=303)


@router.post("/dashboard/brands/{brand_id}/delete")
async def brands_delete(brand_id: int):
    await delete_brand(brand_id)
    return RedirectResponse("/dashboard/brands", status_code=303)


# ── Brand keywords (scoped to a brand) ───────────────────────────

@router.post("/dashboard/brands/{brand_id}/keywords/add")
async def brand_keyword_add(brand_id: int, keyword: str = Form(...)):
    kw = keyword.strip()
    if kw:
        await insert_seed_keyword(brand_id, kw)
    return RedirectResponse(f"/dashboard/brands/{brand_id}/keywords", status_code=303)


@router.post("/dashboard/brands/{brand_id}/keywords/{keyword_id}/toggle")
async def brand_keyword_toggle(brand_id: int, keyword_id: int):
    await toggle_seed_keyword(keyword_id)
    return RedirectResponse(f"/dashboard/brands/{brand_id}/keywords", status_code=303)


@router.post("/dashboard/brands/{brand_id}/keywords/{keyword_id}/delete")
async def brand_keyword_delete(brand_id: int, keyword_id: int):
    await delete_seed_keyword(keyword_id)
    return RedirectResponse(f"/dashboard/brands/{brand_id}/keywords", status_code=303)


# ── Brand social accounts ──────────────────────────────────────

@router.post("/dashboard/brands/{brand_id}/accounts/add")
async def brand_account_add(brand_id: int, request: Request):
    form = await request.form()
    platform = (form.get("platform") or "").strip()
    display_name = (form.get("display_name") or "primary").strip() or "primary"
    if platform not in PLATFORM_FIELDS:
        raise HTTPException(400, f"Unknown platform: {platform}")
    creds = {key: (form.get(f"cred_{key}") or "").strip() for key, _ in PLATFORM_FIELDS[platform]}
    await insert_brand_account(brand_id, platform, display_name, creds)
    return RedirectResponse(f"/dashboard/brands/{brand_id}/social-accounts", status_code=303)


@router.post("/dashboard/brands/{brand_id}/accounts/{account_id}/update")
async def brand_account_update(brand_id: int, account_id: int, request: Request):
    form = await request.form()
    platform = (form.get("platform") or "").strip()
    if platform not in PLATFORM_FIELDS:
        raise HTTPException(400, f"Unknown platform: {platform}")
    creds = {key: (form.get(f"cred_{key}") or "").strip() for key, _ in PLATFORM_FIELDS[platform]}
    await update_brand_account(
        account_id,
        display_name=(form.get("display_name") or "primary").strip() or "primary",
        credentials=creds,
        enabled=bool(form.get("enabled")),
    )
    return RedirectResponse(f"/dashboard/brands/{brand_id}/social-accounts", status_code=303)


@router.post("/dashboard/brands/{brand_id}/accounts/{account_id}/toggle")
async def brand_account_toggle(brand_id: int, account_id: int):
    await toggle_brand_account(account_id)
    return RedirectResponse(f"/dashboard/brands/{brand_id}/social-accounts", status_code=303)


@router.post("/dashboard/brands/{brand_id}/accounts/{account_id}/delete")
async def brand_account_delete(brand_id: int, account_id: int):
    await delete_brand_account(account_id)
    return RedirectResponse(f"/dashboard/brands/{brand_id}/social-accounts", status_code=303)


# ── Pipeline triggers (background) ─────────────────────────────

@router.post("/dashboard/run/mine")
async def run_mine(brand_id: int | None = Form(None)):
    from src.scheduler.jobs import intent_mining_pipeline
    asyncio.create_task(intent_mining_pipeline(brand_id=brand_id))
    target = f"/dashboard/brands/{brand_id}/keywords" if brand_id else "/"
    return RedirectResponse(target, status_code=303)


@router.post("/dashboard/run/pipeline")
async def run_pipeline():
    from src.scheduler.jobs import main_pipeline
    asyncio.create_task(main_pipeline())
    return RedirectResponse("/", status_code=303)


@router.post("/dashboard/run/stage")
async def run_stage(stage: str = Form(...)):
    from src.scheduler.jobs import (
        stage_research,
        stage_generate,
        stage_enrich,
        stage_finalize,
    )
    fns = {
        "research": stage_research,
        "generate": stage_generate,
        "enrich": stage_enrich,
        "finalize": stage_finalize,
    }
    fn = fns.get(stage)
    if not fn:
        raise HTTPException(400, f"Unknown stage: {stage}")
    asyncio.create_task(fn())
    return RedirectResponse("/", status_code=303)


@router.get("/dashboard/publishes", response_class=HTMLResponse)
async def publishes_list(
    request: Request,
    page: int = 1,
    per_page: int = 50,
    sort: str | None = None,
    dir: str | None = None,
):
    sort_cols = {
        "title": "c.title",
        "cluster": "c.cluster",
        "platform": "pl.platform::text",
        "cta_variant": "pl.cta_variant::text",
        "ctr": "COALESCE(p.ctr, 0)",
        "clicks": "COALESCE(p.clicks, 0)",
        "signups": "COALESCE(p.signups, 0)",
        "published_at": "pl.published_at",
    }
    order_by, sort_key, sort_dir = _resolve_sort(sort, dir, sort_cols, "published_at", "desc")

    page, per_page, offset = _page_params(page, per_page)
    total = await _count("SELECT COUNT(*) FROM publish_logs")
    rows = await _fetch(
        f"""
        SELECT pl.content_id, c.title, c.cluster,
               pl.platform::text AS platform, pl.published_url,
               pl.cta_variant::text AS cta_variant, pl.published_at,
               COALESCE(p.ctr, 0) AS ctr,
               COALESCE(p.clicks, 0) AS clicks,
               COALESCE(p.signups, 0) AS signups
        FROM publish_logs pl
        JOIN content c ON pl.content_id = c.content_id
        LEFT JOIN performance p
          ON pl.content_id = p.content_id AND pl.platform = p.platform
        ORDER BY {order_by} LIMIT :lim OFFSET :off
        """,
        {"lim": per_page, "off": offset},
    )
    full_q = f"sort={sort_key}&dir={sort_dir}"
    return templates.TemplateResponse(
        request, "publishes.html",
        {
            "rows": rows, "page": page, "per_page": per_page, "total": total,
            "query": full_q, "filter_query": "",
            "sort_key": sort_key, "sort_dir": sort_dir,
        },
    )


# ── Authentication routes (public) ─────────────────────────────

@public_router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str | None = None):
    if request.session.get("user_id"):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": error})


@public_router.post("/login")
async def login_submit(request: Request, email: str = Form(...), password: str = Form(...)):
    user = await fetch_user_by_email(email.strip().lower())
    if not user or not verify_password(password, user.hashed_password):
        return templates.TemplateResponse(
            request, "login.html",
            {"error": "Invalid email or password", "email": email},
            status_code=401,
        )
    request.session["user_id"] = user.id
    return RedirectResponse("/", status_code=303)


@public_router.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ── Users management (admin only) ──────────────────────────────

@router.get("/dashboard/users", response_class=HTMLResponse)
async def users_list(
    request: Request,
    sort: str | None = None,
    dir: str | None = None,
    _admin: dict = Depends(require_admin),
):
    sort_cols = {
        "email": "email",
        "brand": "brand",
        "created_at": "created_at",
        "updated_at": "updated_at",
    }
    order_by, sort_key, sort_dir = _resolve_sort(sort, dir, sort_cols, "created_at", "asc")
    rows = await _fetch(
        f"""
        SELECT id, email, hashed_password, role, created_at, updated_at
        FROM users ORDER BY {order_by}
        """
    )
    return templates.TemplateResponse(
        request, "users.html",
        {"rows": rows, "sort_key": sort_key, "sort_dir": sort_dir, "filter_query": ""},
    )


@router.post("/dashboard/users/add")
async def users_add(
    email: str = Form(...),
    password: str = Form(...),
    role: str = Form("editor"),
    _admin: dict = Depends(require_admin),
):
    if role not in ("admin", "editor"):
        raise HTTPException(400, "Invalid role")
    if len(password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    existing = await fetch_user_by_email(email.strip().lower())
    if existing:
        raise HTTPException(400, f"User with email {email} already exists")
    await insert_user(email.strip().lower(), hash_password(password), role)
    return RedirectResponse("/dashboard/users", status_code=303)


@router.post("/dashboard/users/{user_id}/update")
async def users_update(
    user_id: int,
    email: str = Form(...),
    role: str = Form(...),
    password: str = Form(""),
    _admin: dict = Depends(require_admin),
):
    if role not in ("admin", "editor"):
        raise HTTPException(400, "Invalid role")
    fields: dict = {"email": email.strip().lower(), "brand": brand}
    if password:
        if len(password) < 8:
            raise HTTPException(400, "Password must be at least 8 characters")
        fields["hashed_password"] = hash_password(password)
    await update_user(user_id, **fields)
    return RedirectResponse("/dashboard/users", status_code=303)


@router.post("/dashboard/users/{user_id}/delete")
async def users_delete(
    user_id: int,
    request: Request,
    admin: dict = Depends(require_admin),
):
    if user_id == admin.id:
        raise HTTPException(400, "Cannot delete yourself")
    # Don't allow deleting the last admin
    all_users = await fetch_users()
    admins = [u for u in all_users if u.role == "admin"]
    target = await fetch_user(user_id)
    if target and target.role == "admin" and len(admins) <= 1:
        raise HTTPException(400, "Cannot delete the last admin")
    await delete_user(user_id)
    return RedirectResponse("/dashboard/users", status_code=303)


# ── Prompts (admin only) ──────────────────────────────────────

async def _seed_known_prompts() -> None:
    """Make sure the known system prompts are present in the DB."""
    from src.content.prompt_store import get_prompt
    from src.content.prompts import CONTENT_SYSTEM, HUMANIZE_SYSTEM, WECHAT_SYSTEM
    await get_prompt("content_system", CONTENT_SYSTEM,
                     name="Content writer (Claude system)",
                     description="System prompt for the article-writing stage.")
    await get_prompt("humanize_system", HUMANIZE_SYSTEM,
                     name="Humanize pass (Claude system)",
                     description="System prompt for the humanization rewrite step.")
    await get_prompt("wechat_system", WECHAT_SYSTEM,
                     name="WeChat converter (Claude system)",
                     description="System prompt for converting articles to WeChat format.")


@router.get("/dashboard/prompts", response_class=HTMLResponse)
async def prompts_root(_admin = Depends(require_admin)):
    await _seed_known_prompts()
    rows = await fetch_prompts()
    if not rows:
        # No prompts at all — render an empty state template
        # (re-use the keyed page with no active prompt)
        return RedirectResponse("/dashboard/prompts/content_system", status_code=303)
    return RedirectResponse(f"/dashboard/prompts/{rows[0].key}", status_code=303)


@router.get("/dashboard/prompts/{key}", response_class=HTMLResponse)
async def prompts_tab(request: Request, key: str, _admin = Depends(require_admin)):
    await _seed_known_prompts()
    rows = await fetch_prompts()
    active_prompt = next((p for p in rows if p.key == key), None)
    if not active_prompt and rows:
        return RedirectResponse(f"/dashboard/prompts/{rows[0].key}", status_code=303)
    return templates.TemplateResponse(
        request, "prompts.html",
        {"rows": rows, "active_prompt": active_prompt, "active_key": key},
    )


@router.post("/dashboard/prompts/{prompt_id}/update")
async def prompts_update(
    prompt_id: int,
    body: str = Form(...),
    key: str = Form(...),
    _admin = Depends(require_admin),
):
    from src.content import prompt_store
    await update_prompt_body(prompt_id, body)
    prompt_store.invalidate()
    return RedirectResponse(f"/dashboard/prompts/{key}", status_code=303)


# ── Settings (admin only) ─────────────────────────────────────

@router.get("/dashboard/settings", response_class=HTMLResponse)
async def settings_list(request: Request, _admin = Depends(require_admin)):
    # Ensure all known runtime settings exist as rows so admins can edit them
    from src import settings_store
    await settings_store.seed_known()
    rows = await fetch_settings()
    return templates.TemplateResponse(request, "settings.html", {"rows": rows})


@router.post("/dashboard/settings/add")
async def settings_add(
    key: str = Form(...),
    value: str = Form(""),
    description: str = Form(""),
    _admin = Depends(require_admin),
):
    from src import settings_store
    await upsert_setting(key.strip(), value, description.strip())
    settings_store.invalidate(key.strip())
    return RedirectResponse("/dashboard/settings", status_code=303)


@router.post("/dashboard/settings/{setting_id}/update")
async def settings_update(
    setting_id: int,
    value: str = Form(""),
    _admin = Depends(require_admin),
):
    from src import settings_store
    await update_setting_value(setting_id, value)
    settings_store.invalidate()  # don't know the key from id, drop all
    return RedirectResponse("/dashboard/settings", status_code=303)


@router.post("/dashboard/settings/{setting_id}/delete")
async def settings_delete(setting_id: int, _admin = Depends(require_admin)):
    from src import settings_store
    await delete_setting(setting_id)
    settings_store.invalidate()
    return RedirectResponse("/dashboard/settings", status_code=303)

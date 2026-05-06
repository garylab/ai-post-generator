from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text

from src.storage.database import (
    delete_role,
    delete_role_account,
    delete_seed_keyword,
    delete_user,
    fetch_content,
    fetch_role,
    fetch_role_accounts,
    fetch_roles,
    fetch_seed_keywords,
    fetch_user,
    fetch_user_by_email,
    fetch_users,
    get_session,
    insert_role,
    insert_role_account,
    insert_seed_keyword,
    insert_user,
    toggle_role_account,
    toggle_seed_keyword,
    update_role,
    update_role_account,
    update_user,
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
):
    where = []
    params: dict = {}
    if status:
        where.append("status::text = :status")
        params["status"] = status
    if cluster:
        where.append("cluster = :cluster")
        params["cluster"] = cluster
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    page, per_page, offset = _page_params(page, per_page)
    total = await _count(f"SELECT COUNT(*) FROM content {clause}", params)
    rows = await _fetch(
        f"""
        SELECT content_id, title, cluster, status::text AS status, score,
               iteration_count, created_at, updated_at
        FROM content {clause}
        ORDER BY created_at DESC LIMIT :lim OFFSET :off
        """,
        {**params, "lim": per_page, "off": offset},
    )
    statuses = await _fetch(
        "SELECT DISTINCT status::text AS s FROM content ORDER BY s"
    )
    clusters = await _fetch(
        "SELECT DISTINCT cluster AS c FROM content WHERE cluster IS NOT NULL ORDER BY c"
    )
    qs = []
    if status: qs.append(f"status={status}")
    if cluster: qs.append(f"cluster={cluster}")
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
            "query": "&".join(qs),
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
):
    where = ""
    params: dict = {}
    if status:
        where = "WHERE i.status::text = :status"
        params["status"] = status
    page, per_page, offset = _page_params(page, per_page)
    total = await _count(
        f"SELECT COUNT(*) FROM intents i LEFT JOIN intent_clusters ic ON i.cluster_id = ic.id {where}",
        params,
    )
    rows = await _fetch(
        f"""
        SELECT i.id, i.title, i.source, i.status::text AS status,
               i.priority_score, i.is_pillar, i.created_at,
               ic.slug AS cluster_slug
        FROM intents i LEFT JOIN intent_clusters ic ON i.cluster_id = ic.id
        {where}
        ORDER BY i.created_at DESC LIMIT :lim OFFSET :off
        """,
        {**params, "lim": per_page, "off": offset},
    )
    statuses = await _fetch("SELECT DISTINCT status::text AS s FROM intents ORDER BY s")
    return templates.TemplateResponse(
        request,
        "intents.html",
        {
            "rows": rows,
            "statuses": [r["s"] for r in statuses],
            "selected_status": status,
            "page": page, "per_page": per_page, "total": total,
            "query": (f"status={status}" if status else ""),
        },
    )


@router.get("/dashboard/clusters/{cluster_id}/intents.json")
async def cluster_intents_json(cluster_id: int):
    rows = await _fetch(
        """
        SELECT i.id, i.title, i.source, i.status::text AS status,
               i.priority_score, i.is_pillar, i.created_at
        FROM intents i WHERE i.cluster_id = :cid
        ORDER BY i.is_pillar DESC, i.priority_score DESC
        """,
        {"cid": cluster_id},
    )
    # Stringify timestamps for JSON
    for r in rows:
        if r.get("created_at"):
            r["created_at"] = r["created_at"].isoformat()
        if r.get("priority_score") is not None:
            r["priority_score"] = float(r["priority_score"])
    return rows


@router.post("/dashboard/clusters/{cluster_id}/generate")
async def cluster_generate(cluster_id: int):
    from src.scheduler.jobs import run_pipeline_for_cluster
    asyncio.create_task(run_pipeline_for_cluster(cluster_id))
    return RedirectResponse("/dashboard/clusters", status_code=303)


@router.get("/dashboard/clusters", response_class=HTMLResponse)
async def clusters_list(request: Request, page: int = 1, per_page: int = 50):
    page, per_page, offset = _page_params(page, per_page)
    total = await _count("SELECT COUNT(*) FROM intent_clusters")
    rows = await _fetch(
        """
        SELECT id, name, slug, status::text AS status,
               intent_count, covered_count, priority_score, created_at
        FROM intent_clusters
        ORDER BY priority_score DESC, created_at DESC
        LIMIT :lim OFFSET :off
        """,
        {"lim": per_page, "off": offset},
    )
    return templates.TemplateResponse(
        request, "clusters.html",
        {"rows": rows, "page": page, "per_page": per_page, "total": total, "query": ""},
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


@router.get("/dashboard/roles", response_class=HTMLResponse)
async def roles_list(request: Request, page: int = 1, per_page: int = 50):
    page, per_page, offset = _page_params(page, per_page)
    total = await _count("SELECT COUNT(*) FROM roles")
    rows = await _fetch(
        """
        SELECT id, slug, name, description, enabled, created_at
        FROM roles ORDER BY created_at ASC LIMIT :lim OFFSET :off
        """,
        {"lim": per_page, "off": offset},
    )
    counts = await _fetch(
        """
        SELECT r.id,
               (SELECT COUNT(*) FROM seed_keywords WHERE role_id = r.id) AS keyword_count,
               (SELECT COUNT(*) FROM role_social_accounts WHERE role_id = r.id) AS account_count,
               (SELECT COUNT(*) FROM content WHERE role_id = r.id) AS content_count
        FROM roles r
        """
    )
    counts_by_id = {c["id"]: c for c in counts}
    return templates.TemplateResponse(
        request, "roles.html",
        {
            "rows": rows, "counts": counts_by_id,
            "page": page, "per_page": per_page, "total": total, "query": "",
        },
    )


@router.post("/dashboard/roles/add")
async def roles_add(name: str = Form(...), description: str = Form("")):
    rid = await insert_role(name.strip(), description.strip())
    return RedirectResponse(f"/dashboard/roles/{rid}", status_code=303)


@router.get("/dashboard/roles/{role_id}", response_class=HTMLResponse)
async def role_detail_root(role_id: int):
    return RedirectResponse(f"/dashboard/roles/{role_id}/overall", status_code=303)


async def _role_or_404(role_id: int) -> dict:
    role = await fetch_role(role_id)
    if not role:
        raise HTTPException(404, "Role not found")
    return role


@router.get("/dashboard/roles/{role_id}/overall", response_class=HTMLResponse)
async def role_tab_overall(request: Request, role_id: int):
    role = await _role_or_404(role_id)
    return templates.TemplateResponse(
        request, "role_detail.html",
        {"role": role, "active_tab": "overall"},
    )


@router.get("/dashboard/roles/{role_id}/social-accounts", response_class=HTMLResponse)
async def role_tab_accounts(request: Request, role_id: int):
    role = await _role_or_404(role_id)
    accounts = await fetch_role_accounts(role_id)
    return templates.TemplateResponse(
        request, "role_detail.html",
        {
            "role": role,
            "active_tab": "social-accounts",
            "accounts": accounts,
            "platform_fields": PLATFORM_FIELDS,
        },
    )


@router.get("/dashboard/roles/{role_id}/keywords", response_class=HTMLResponse)
async def role_tab_keywords(request: Request, role_id: int):
    role = await _role_or_404(role_id)
    keywords = await fetch_seed_keywords(role_id=role_id)
    return templates.TemplateResponse(
        request, "role_detail.html",
        {"role": role, "active_tab": "keywords", "keywords": keywords},
    )


@router.post("/dashboard/roles/{role_id}/update")
async def roles_update(
    role_id: int,
    name: str = Form(...),
    description: str = Form(""),
    enabled: str = Form(None),
):
    await update_role(
        role_id,
        name=name.strip(),
        description=description.strip(),
        enabled=bool(enabled),
    )
    return RedirectResponse(f"/dashboard/roles/{role_id}/overall", status_code=303)


@router.post("/dashboard/roles/{role_id}/delete")
async def roles_delete(role_id: int):
    await delete_role(role_id)
    return RedirectResponse("/dashboard/roles", status_code=303)


# ── Role keywords (scoped to a role) ───────────────────────────

@router.post("/dashboard/roles/{role_id}/keywords/add")
async def role_keyword_add(role_id: int, keyword: str = Form(...)):
    kw = keyword.strip()
    if kw:
        await insert_seed_keyword(role_id, kw)
    return RedirectResponse(f"/dashboard/roles/{role_id}/keywords", status_code=303)


@router.post("/dashboard/roles/{role_id}/keywords/{keyword_id}/toggle")
async def role_keyword_toggle(role_id: int, keyword_id: int):
    await toggle_seed_keyword(keyword_id)
    return RedirectResponse(f"/dashboard/roles/{role_id}/keywords", status_code=303)


@router.post("/dashboard/roles/{role_id}/keywords/{keyword_id}/delete")
async def role_keyword_delete(role_id: int, keyword_id: int):
    await delete_seed_keyword(keyword_id)
    return RedirectResponse(f"/dashboard/roles/{role_id}/keywords", status_code=303)


# ── Role social accounts ──────────────────────────────────────

@router.post("/dashboard/roles/{role_id}/accounts/add")
async def role_account_add(role_id: int, request: Request):
    form = await request.form()
    platform = (form.get("platform") or "").strip()
    display_name = (form.get("display_name") or "primary").strip() or "primary"
    if platform not in PLATFORM_FIELDS:
        raise HTTPException(400, f"Unknown platform: {platform}")
    creds = {key: (form.get(f"cred_{key}") or "").strip() for key, _ in PLATFORM_FIELDS[platform]}
    await insert_role_account(role_id, platform, display_name, creds)
    return RedirectResponse(f"/dashboard/roles/{role_id}/social-accounts", status_code=303)


@router.post("/dashboard/roles/{role_id}/accounts/{account_id}/update")
async def role_account_update(role_id: int, account_id: int, request: Request):
    form = await request.form()
    platform = (form.get("platform") or "").strip()
    if platform not in PLATFORM_FIELDS:
        raise HTTPException(400, f"Unknown platform: {platform}")
    creds = {key: (form.get(f"cred_{key}") or "").strip() for key, _ in PLATFORM_FIELDS[platform]}
    await update_role_account(
        account_id,
        display_name=(form.get("display_name") or "primary").strip() or "primary",
        credentials=creds,
        enabled=bool(form.get("enabled")),
    )
    return RedirectResponse(f"/dashboard/roles/{role_id}/social-accounts", status_code=303)


@router.post("/dashboard/roles/{role_id}/accounts/{account_id}/toggle")
async def role_account_toggle(role_id: int, account_id: int):
    await toggle_role_account(account_id)
    return RedirectResponse(f"/dashboard/roles/{role_id}/social-accounts", status_code=303)


@router.post("/dashboard/roles/{role_id}/accounts/{account_id}/delete")
async def role_account_delete(role_id: int, account_id: int):
    await delete_role_account(account_id)
    return RedirectResponse(f"/dashboard/roles/{role_id}/social-accounts", status_code=303)


# ── Pipeline triggers (background) ─────────────────────────────

@router.post("/dashboard/run/mine")
async def run_mine(role_id: int | None = Form(None)):
    from src.scheduler.jobs import intent_mining_pipeline
    asyncio.create_task(intent_mining_pipeline(role_id=role_id))
    target = f"/dashboard/roles/{role_id}/overall" if role_id else "/"
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
async def publishes_list(request: Request, page: int = 1, per_page: int = 50):
    page, per_page, offset = _page_params(page, per_page)
    total = await _count("SELECT COUNT(*) FROM publish_logs")
    rows = await _fetch(
        """
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
        ORDER BY pl.published_at DESC LIMIT :lim OFFSET :off
        """,
        {"lim": per_page, "off": offset},
    )
    return templates.TemplateResponse(
        request, "publishes.html",
        {"rows": rows, "page": page, "per_page": per_page, "total": total, "query": ""},
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
async def users_list(request: Request, _admin: dict = Depends(require_admin)):
    rows = await fetch_users()
    return templates.TemplateResponse(request, "users.html", {"rows": rows})


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
    fields: dict = {"email": email.strip().lower(), "role": role}
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

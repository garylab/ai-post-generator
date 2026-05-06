from __future__ import annotations

import hashlib
import hmac

import httpx
import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Header, HTTPException, Request

from starlette.middleware.sessions import SessionMiddleware

from src.approval.webhook_server import router as webhook_router, set_publish_callback
from src.config import settings
from src.scheduler.jobs import daily_metrics, main_pipeline, publish_approved
from src.storage.database import close_db, init_db, ping
from src.utils.logging import setup_logging
from src.web.auth import install_login_redirect
from src.web.dashboard import public_router as dashboard_public_router, router as dashboard_router
from loguru import logger as log


app = FastAPI(title="Mockreal Growth Engine", version="1.0.0")
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    session_cookie="apg_session",
    max_age=60 * 60 * 24 * 14,  # 14 days
    same_site="lax",
)
install_login_redirect(app)
app.include_router(webhook_router)
app.include_router(dashboard_public_router)
app.include_router(dashboard_router)


# ── Health ───────────────────────────────────────────────────

@app.get("/health")
async def health():
    db_ok = await ping()
    status = "ok" if db_ok else "degraded"
    return {"status": status, "db": db_ok}


# ── Manual trigger ───────────────────────────────────────────

@app.post("/trigger/pipeline")
async def trigger_pipeline(x_api_key: str = Header(None)):
    if x_api_key != settings.openai_api_key[:20]:
        raise HTTPException(403, "Invalid API key")
    import asyncio
    asyncio.create_task(main_pipeline())
    return {"status": "triggered", "message": "Pipeline started in background"}


@app.post("/trigger/metrics")
async def trigger_metrics(x_api_key: str = Header(None)):
    if x_api_key != settings.openai_api_key[:20]:
        raise HTTPException(403, "Invalid API key")
    import asyncio
    asyncio.create_task(daily_metrics())
    return {"status": "triggered", "message": "Metrics job started in background"}


# ── Telegram webhook verification ────────────────────────────

async def _register_telegram_webhook() -> None:
    """Register this server's URL as Telegram webhook endpoint."""
    if not settings.telegram_bot_token:
        log.warning("Telegram bot token not set, skipping webhook registration")
        return

    webhook_url = f"http://localhost:{settings.app_port}/webhook/telegram"

    secret = hashlib.sha256(settings.telegram_bot_token.encode()).hexdigest()[:32]
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/setWebhook"

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, json={
                "url": webhook_url,
                "secret_token": secret,
                "allowed_updates": ["callback_query"],
            })
            data = resp.json()
            if data.get("ok"):
                log.info("Telegram webhook registered: {}", webhook_url)
            else:
                log.warning("Telegram webhook registration: {}", data.get("description", "unknown error"))
    except Exception as exc:
        log.warning("Failed to register Telegram webhook: {} (set publicly accessible URL in production)", exc)


# ── Lifecycle ────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    setup_logging("INFO")
    log.info("Starting Mockreal Growth Engine...")

    await init_db()
    log.info("Database connected")

    set_publish_callback(publish_approved)

    await _register_telegram_webhook()

    scheduler = AsyncIOScheduler(timezone=settings.generic_timezone)

    scheduler.add_job(
        main_pipeline,
        CronTrigger(hour=f"*/{settings.pipeline_interval_hours}"),
        id="main_pipeline",
        name="Main content pipeline",
        misfire_grace_time=3600,
    )

    scheduler.add_job(
        daily_metrics,
        CronTrigger(hour=settings.metrics_hour),
        id="daily_metrics",
        name="Daily metrics collection",
        misfire_grace_time=3600,
    )

    scheduler.start()
    log.info(
        "Scheduler started: pipeline every {}h, metrics at {}:00 ({})",
        settings.pipeline_interval_hours,
        settings.metrics_hour,
        settings.generic_timezone,
    )


@app.on_event("shutdown")
async def shutdown():
    await close_db()
    log.info("Shutdown complete")


def main():
    setup_logging("INFO")
    uvicorn.run(
        "src.main:app",
        host="0.0.0.0",
        port=settings.app_port,
        log_level="info",
    )


def dev():
    """Local hot-reload dev server."""
    setup_logging("INFO")
    uvicorn.run(
        "src.main:app",
        host="127.0.0.1",
        port=settings.app_port,
        log_level="info",
        reload=True,
        reload_dirs=["src"],
    )


if __name__ == "__main__":
    main()

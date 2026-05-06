"""Direct pipeline runner — bypasses FastAPI for testing.

Usage:
    uv run python -m src.run_pipeline                # Run all stages (research → generate → enrich → finalize)
    uv run python -m src.run_pipeline --mine         # Run intent mining
    uv run python -m src.run_pipeline --grow         # Run growth loop (expand covered clusters)
    uv run python -m src.run_pipeline --research     # Only: pending intents → researched content
    uv run python -m src.run_pipeline --generate     # Only: researched → generated (article + social)
    uv run python -m src.run_pipeline --enrich       # Only: generated → enriched (images + wechat)
    uv run python -m src.run_pipeline --finalize     # Only: enriched → draft → approve/publish
    uv run python -m src.run_pipeline --all          # Mine intents, then run all stages
"""
import asyncio
import sys

from src.storage.database import init_db, close_db
from src.scheduler.jobs import (
    intent_mining_pipeline,
    main_pipeline,
    growth_loop,
    stage_research,
    stage_generate,
    stage_enrich,
    stage_finalize,
)
from src.utils.logging import setup_logging


async def run():
    setup_logging("INFO")
    await init_db()
    print("DB connected.")

    args = set(sys.argv[1:])

    if "--all" in args:
        print("Running intent mining → all production stages...")
        await intent_mining_pipeline()
        await main_pipeline()
    elif "--mine" in args:
        print("Running intent mining...")
        await intent_mining_pipeline()
    elif "--grow" in args:
        print("Running growth loop...")
        await growth_loop()
    elif "--research" in args:
        print("Running stage: research...")
        n = await stage_research()
        print(f"Researched {n} intents.")
    elif "--generate" in args:
        print("Running stage: generate...")
        n = await stage_generate()
        print(f"Generated {n} articles.")
    elif "--enrich" in args:
        print("Running stage: enrich...")
        n = await stage_enrich()
        print(f"Enriched {n} articles.")
    elif "--finalize" in args:
        print("Running stage: finalize...")
        n = await stage_finalize()
        print(f"Finalized {n} articles.")
    else:
        print("Running all production stages (research → generate → enrich → finalize)...")
        await main_pipeline()

    print("Done.")
    await close_db()

if __name__ == "__main__":
    asyncio.run(run())

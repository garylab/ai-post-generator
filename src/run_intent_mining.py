"""Direct intent mining runner — bypasses FastAPI for testing."""
import asyncio
from src.storage.database import init_db, close_db
from src.scheduler.jobs import intent_mining_pipeline
from src.utils.logging import setup_logging

async def run():
    setup_logging("INFO")
    await init_db()
    print("DB connected. Running intent mining pipeline...")
    await intent_mining_pipeline()
    print("Intent mining done.")
    await close_db()

if __name__ == "__main__":
    asyncio.run(run())

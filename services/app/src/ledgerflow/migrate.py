"""Entrypoint for the schema migration Job / initContainer: python -m ledgerflow.migrate"""
import asyncio

from .config import Settings
from .db import migrate, open_pool


async def main() -> None:
    settings = Settings()
    pool = await open_pool(settings.database_url, 1, 2)
    try:
        await migrate(pool)
        print("migration complete")
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())

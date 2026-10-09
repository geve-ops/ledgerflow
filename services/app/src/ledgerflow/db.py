from importlib import resources

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

MIGRATION_LOCK_ID = 7_354_021


async def open_pool(url: str, min_size: int, max_size: int) -> AsyncConnectionPool:
    pool = AsyncConnectionPool(
        url,
        min_size=min_size,
        max_size=max_size,
        open=False,
        kwargs={"row_factory": dict_row},
    )
    await pool.open()
    await pool.wait()
    return pool


async def migrate(pool: AsyncConnectionPool) -> None:
    """Apply schema.sql; the advisory lock makes concurrent runs safe."""
    sql = resources.files("ledgerflow").joinpath("schema.sql").read_text()
    async with pool.connection() as conn:
        await conn.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK_ID,))
        try:
            await conn.execute(sql)
        finally:
            await conn.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_LOCK_ID,))

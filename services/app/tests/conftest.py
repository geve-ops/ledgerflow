import asyncio
import sys
import uuid

import httpx
import pytest
import pytest_asyncio
from testcontainers.postgres import PostgresContainer
from testcontainers.redis import RedisContainer

from ledgerflow.api.main import create_app
from ledgerflow.config import Settings
from ledgerflow.db import migrate, open_pool
from ledgerflow.redis_client import make_redis
from ledgerflow.worker.main import Worker

# psycopg async cannot run on Windows' default ProactorEventLoop (local dev only;
# the container image is Linux).
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


@pytest.fixture(scope="session")
def pg_url():
    with PostgresContainer("postgres:16-alpine", driver=None) as pg:
        yield pg.get_connection_url()


@pytest.fixture(scope="session")
def redis_url():
    with RedisContainer("redis:7-alpine") as r:
        yield f"redis://{r.get_container_host_ip()}:{r.get_exposed_port(6379)}/0"


@pytest.fixture
def settings(pg_url, redis_url):
    # Unique stream/group per test keeps tests independent on shared containers.
    suffix = uuid.uuid4().hex[:8]
    return Settings(
        database_url=pg_url,
        redis_url=redis_url,
        api_keys="test-key:acme,other-key:globex",
        rate_limit_per_minute=1000,
        stream=f"ledger:events:{suffix}",
        dlq_stream=f"ledger:dlq:{suffix}",
        group="test-workers",
        claim_idle_ms=50,
        max_deliveries=3,
    )


@pytest_asyncio.fixture
async def pool(settings):
    p = await open_pool(settings.database_url, 1, 5)
    await migrate(p)
    yield p
    await p.close()


@pytest_asyncio.fixture
async def redis(settings):
    r = make_redis(settings)
    yield r
    await r.aclose()


@pytest_asyncio.fixture
async def worker(settings, pool, redis):
    w = Worker(settings, pool, redis, consumer="test-consumer")
    await w.ensure_group()
    return w


@pytest_asyncio.fixture
async def client(settings, pool):
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", headers={"X-API-Key": "test-key"}
        ) as c:
            yield c


def new_id(prefix: str = "acct") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


async def drain(worker: Worker, attempts: int = 5) -> None:
    for _ in range(attempts):
        if await worker.run_once(block_ms=100) == 0:
            await asyncio.sleep(0.06)

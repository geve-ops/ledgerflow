import redis.asyncio as aioredis

from .config import Settings


def make_redis(settings: Settings) -> aioredis.Redis:
    return aioredis.from_url(
        settings.redis_url,
        password=settings.redis_password,
        decode_responses=True,
    )

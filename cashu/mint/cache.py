import functools
import json
from typing import Any, Union

from fastapi import Request
from loguru import logger
from pydantic import BaseModel
from redis.asyncio import Redis, from_url
from redis.asyncio.cluster import RedisCluster
from redis.exceptions import ConnectionError

from ..core.errors import CashuError
from ..core.settings import settings


class RedisCache:
    # NUT #19: Any Mint implementation should elect a data structure `D` that maps request objects to their respective responses. `D` should be fit for fast insertion, look-up and deletion (eviction) operations. This could be an in-memory database or a dedicated caching service like Redis.
    initialized = False
    redis: Union[Redis, Any]

    def __init__(self):
        if settings.mint_redis_cache_enabled:
            if settings.mint_redis_cache_url is None:
                raise CashuError("Redis cache url not provided")
            if settings.mint_redis_cache_cluster:
                self.redis = RedisCluster.from_url(settings.mint_redis_cache_url)
            else:
                self.redis = from_url(settings.mint_redis_cache_url)

    async def test_connection(self):
        # PING
        try:
            await self.redis.ping()
            logger.success("Connected to Redis caching server.")
            self.initialized = True
        except ConnectionError as e:
            logger.error("Redis connection error.")
            raise e

    def cache(self):
        def passthrough(func):
            @functools.wraps(func)
            async def wrapper(*args, **kwargs):
                logger.trace(f"cache wrapper on route {func.__name__}")
                result = await func(*args, **kwargs)
                return result

            return wrapper

        def decorator(func):
            @functools.wraps(func)
            async def wrapper(request: Request, payload: BaseModel):
                logger.trace(f"cache wrapper on route {func.__name__}")
                # NUT #19: Upon receiving a `request` on a cached endpoint, the mint derives a unique key `k` for it which should depend on the method, path, and the payload of `request`.
                key = request.url.path + payload.model_dump_json()
                logger.trace(f"KEY: {key}")
                # Check if we have a value under this key
                if await self.redis.exists(key):
                    logger.trace("Returning a cached response...")
                    # NUT #19: If a cached `response` is found: `request` has a matching `response`. The mint returns the cached `response`.
                    resp = await self.redis.get(key)
                    if resp:
                        return json.loads(resp)
                    else:
                        raise Exception(f"Found no cached response for key {key}")
                result = await func(request, payload)
                # NUT #19: For each successful response on a cached endpoint (`status_code == 200`), the mint stores the response in `D` under key `k` (`D[k] = response`).
                # NUT #19: The mint decides the `ttl` (Time To Live) of cached response, after which it can evict the entry from `D`.
                await self.redis.set(name=key, value=result.model_dump_json(), ex=settings.mint_redis_cache_ttl)
                return result

            return wrapper

        return passthrough if not settings.mint_redis_cache_enabled else decorator

    async def disconnect(self):
        if self.initialized:
            await self.redis.close()

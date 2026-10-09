"""Stream consumer: python -m ledgerflow.worker.main"""
import asyncio
import json
import logging
import os
import signal
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import redis.asyncio as aioredis
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from psycopg_pool import AsyncConnectionPool
from redis.exceptions import ResponseError

from .. import metrics
from ..config import Settings
from ..db import open_pool
from ..posting import post_transaction
from ..redis_client import make_redis

log = logging.getLogger("ledgerflow.worker")


class Worker:
    def __init__(
        self,
        settings: Settings,
        pool: AsyncConnectionPool,
        redis: aioredis.Redis,
        consumer: str | None = None,
    ):
        self.s = settings
        self.pool = pool
        self.redis = redis
        self.consumer = consumer or f"{socket.gethostname()}-{os.getpid()}"
        self.last_beat = time.monotonic()

    async def ensure_group(self) -> None:
        try:
            await self.redis.xgroup_create(self.s.stream, self.s.group, id="0", mkstream=True)
        except ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise

    async def run_once(self, block_ms: int = 2000) -> int:
        """Reclaim stale entries, then read new ones. Returns messages handled."""
        handled = 0
        _, claimed, _ = await self.redis.xautoclaim(
            self.s.stream, self.s.group, self.consumer,
            min_idle_time=self.s.claim_idle_ms, start_id="0-0", count=self.s.batch_size,
        )
        for msg_id, fields in claimed:
            await self._handle(msg_id, fields)
            handled += 1

        resp = await self.redis.xreadgroup(
            self.s.group, self.consumer, {self.s.stream: ">"},
            count=self.s.batch_size, block=block_ms,
        )
        for _, messages in resp or []:
            for msg_id, fields in messages:
                await self._handle(msg_id, fields)
                handled += 1

        await self._update_lag()
        self.last_beat = time.monotonic()
        return handled

    async def _handle(self, msg_id: str, fields: dict) -> None:
        try:
            ev = json.loads(fields["data"])
        except (KeyError, ValueError):
            await self._dead_letter(msg_id, fields, "malformed_event")
            return
        try:
            with metrics.POSTING_LATENCY.time():
                status, reason = await post_transaction(self.pool, ev)
        except Exception:
            # Leave the entry pending: XAUTOCLAIM redelivers it after claim_idle_ms.
            metrics.PROCESSED.labels(status="error").inc()
            log.exception("posting failed for %s", ev.get("id"))
            if await self._deliveries(msg_id) >= self.s.max_deliveries:
                await self._dead_letter(msg_id, fields, "max_deliveries_exceeded")
                await self._set_status(ev["id"], "failed", "max_deliveries_exceeded")
            return

        metrics.PROCESSED.labels(status=status).inc()
        if status != "duplicate":
            await self._set_status(ev["id"], status, reason)
        await self.redis.xack(self.s.stream, self.s.group, msg_id)

    async def _deliveries(self, msg_id: str) -> int:
        rows = await self.redis.xpending_range(
            self.s.stream, self.s.group, min=msg_id, max=msg_id, count=1
        )
        return rows[0]["times_delivered"] if rows else 0

    async def _dead_letter(self, msg_id: str, fields: dict, reason: str) -> None:
        await self.redis.xadd(self.s.dlq_stream, {**fields, "dlq_reason": reason})
        await self.redis.xack(self.s.stream, self.s.group, msg_id)
        metrics.PROCESSED.labels(status="dead_lettered").inc()

    async def _set_status(self, txn_id: str, status: str, reason: str | None) -> None:
        key = f"txn:{txn_id}"
        await self.redis.hset(key, mapping={"status": status, "reason": reason or ""})
        await self.redis.expire(key, self.s.txn_status_ttl_seconds)

    async def _update_lag(self) -> None:
        for g in await self.redis.xinfo_groups(self.s.stream):
            if g["name"] == self.s.group:
                metrics.STREAM_LAG.set(g.get("lag") or 0)
                metrics.STREAM_PENDING.set(g.get("pending") or 0)


def start_http_server(worker: Worker, port: int) -> ThreadingHTTPServer:
    """/metrics for Prometheus and /healthz for probes (fails if the loop stalls)."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path == "/metrics":
                body, code, ctype = generate_latest(), 200, CONTENT_TYPE_LATEST
            elif self.path == "/healthz":
                ok = time.monotonic() - worker.last_beat < 30
                body, code, ctype = (b"ok" if ok else b"stalled"), (200 if ok else 503), "text/plain"
            else:
                body, code, ctype = b"not found", 404, "text/plain"
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings()
    pool = await open_pool(settings.database_url, settings.db_pool_min, settings.db_pool_max)
    redis = make_redis(settings)
    worker = Worker(settings, pool, redis)
    await worker.ensure_group()
    server = start_http_server(worker, settings.worker_metrics_port)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            pass

    log.info("worker %s started", worker.consumer)
    while not stop.is_set():
        try:
            await worker.run_once()
        except Exception:
            log.exception("loop error")
            await asyncio.sleep(1)

    log.info("shutting down")
    server.shutdown()
    await pool.close()
    await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())

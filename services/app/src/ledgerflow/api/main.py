"""HTTP API: uvicorn ledgerflow.api.main:app"""
import hmac
import json
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from psycopg.errors import UniqueViolation

from .. import metrics
from ..config import Settings
from ..db import open_pool
from ..models import AccountIn, TransactionIn
from ..redis_client import make_redis


def create_app(settings: Settings | None = None) -> FastAPI:
    cfg = settings or Settings()
    keys = cfg.api_key_map()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.redis = make_redis(cfg)
        app.state.pool = await open_pool(cfg.database_url, cfg.db_pool_min, cfg.db_pool_max)
        app.state.ro_pool = (
            await open_pool(cfg.database_ro_url, cfg.db_pool_min, cfg.db_pool_max)
            if cfg.database_ro_url
            else app.state.pool
        )
        yield
        if app.state.ro_pool is not app.state.pool:
            await app.state.ro_pool.close()
        await app.state.pool.close()
        await app.state.redis.aclose()

    app = FastAPI(title="ledgerflow", version="0.1.0", lifespan=lifespan)

    @app.middleware("http")
    async def observe(request: Request, call_next):
        start = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            route = request.scope.get("route")
            path = route.path if route else "unmatched"
            if path not in ("/metrics", "/healthz", "/readyz"):
                metrics.API_REQUESTS.inc()
                metrics.HTTP_REQUESTS.labels(request.method, path, str(status)).inc()
                metrics.HTTP_LATENCY.labels(request.method, path).observe(
                    time.perf_counter() - start
                )

    async def authenticate(request: Request, x_api_key: str | None = Header(default=None)) -> str:
        client = None
        if x_api_key:
            for key, client_id in keys.items():
                if hmac.compare_digest(key, x_api_key):
                    client = client_id
        if client is None:
            raise HTTPException(401, "invalid or missing X-API-Key")

        # Fixed-window rate limit per client, shared across API replicas via Redis.
        window = int(time.time() // 60)
        rkey = f"rl:{client}:{window}"
        async with request.app.state.redis.pipeline(transaction=True) as pipe:
            pipe.incr(rkey)
            pipe.expire(rkey, 70, nx=True)
            count, _ = await pipe.execute()
        if count > cfg.rate_limit_per_minute:
            metrics.RATE_LIMITED.labels(client).inc()
            retry_after = 60 - int(time.time() % 60)
            raise HTTPException(429, "rate limit exceeded", headers={"Retry-After": str(retry_after)})
        return client

    # ---- probes & metrics -------------------------------------------------
    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz(request: Request):
        try:
            await request.app.state.redis.ping()
            async with request.app.state.pool.connection() as conn:
                await conn.execute("SELECT 1")
        except Exception as e:
            raise HTTPException(503, f"not ready: {type(e).__name__}")
        return {"status": "ready"}

    @app.get("/metrics", include_in_schema=False)
    async def prom():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    # ---- accounts ---------------------------------------------------------
    @app.post("/v1/accounts", status_code=201)
    async def create_account(body: AccountIn, request: Request, client: str = Depends(authenticate)):
        try:
            async with request.app.state.pool.connection() as conn:
                async with conn.transaction():
                    await conn.execute(
                        "INSERT INTO accounts (id, currency, allow_negative) VALUES (%s, %s, %s)",
                        (body.id, body.currency, body.allow_negative),
                    )
                    await conn.execute(
                        "INSERT INTO audit_log (actor, action, entity, entity_id, details) "
                        "VALUES (%s, 'account.created', 'account', %s, %s::jsonb)",
                        (client, body.id, json.dumps(body.model_dump())),
                    )
        except UniqueViolation:
            raise HTTPException(409, "account already exists")
        return {"id": body.id, "currency": body.currency, "balance": 0}

    @app.get("/v1/accounts/{account_id}")
    async def get_account(account_id: str, request: Request, _: str = Depends(authenticate)):
        async with request.app.state.ro_pool.connection() as conn:
            cur = await conn.execute(
                "SELECT id, currency, balance, allow_negative FROM accounts WHERE id = %s",
                (account_id,),
            )
            row = await cur.fetchone()
        if row is None:
            raise HTTPException(404, "account not found")
        return row

    # ---- transactions -----------------------------------------------------
    @app.post("/v1/transactions", status_code=202)
    async def submit_transaction(
        body: TransactionIn,
        request: Request,
        response: Response,
        client: str = Depends(authenticate),
        idempotency_key: str | None = Header(default=None, min_length=8, max_length=128),
    ):
        if not idempotency_key:
            raise HTTPException(400, "Idempotency-Key header is required (8-128 chars)")
        redis = request.app.state.redis
        txn_id = str(uuid.uuid4())
        fingerprint = body.fingerprint()
        idem_key = f"idem:{client}:{idempotency_key}"

        claimed = await redis.set(
            idem_key, f"{txn_id}|{fingerprint}", nx=True, ex=cfg.idempotency_ttl_seconds
        )
        if not claimed:
            existing_id, existing_fp = (await redis.get(idem_key)).split("|", 1)
            if existing_fp != fingerprint:
                raise HTTPException(409, "Idempotency-Key reused with a different request body")
            metrics.IDEMPOTENT_REPLAYS.inc()
            metrics.SUBMITTED.labels("replayed").inc()
            response.status_code = 200
            return {"transaction_id": existing_id, "replayed": True}

        event = {
            "id": txn_id,
            "client_id": client,
            "idempotency_key": idempotency_key,
            "submitted_at": time.time(),
            **body.model_dump(),
        }
        try:
            async with redis.pipeline(transaction=True) as pipe:
                pipe.hset(f"txn:{txn_id}", mapping={"status": "pending", "reason": ""})
                pipe.expire(f"txn:{txn_id}", cfg.txn_status_ttl_seconds)
                pipe.xadd(cfg.stream, {"data": json.dumps(event)})
                await pipe.execute()
        except Exception:
            await redis.delete(idem_key)  # let the client retry with the same key
            metrics.SUBMITTED.labels("enqueue_failed").inc()
            raise HTTPException(503, "could not enqueue transaction")

        metrics.SUBMITTED.labels("accepted").inc()
        return {"transaction_id": txn_id, "status": "pending", "replayed": False}

    @app.get("/v1/transactions/{txn_id}")
    async def get_transaction(txn_id: uuid.UUID, request: Request, _: str = Depends(authenticate)):
        async with request.app.state.ro_pool.connection() as conn:
            cur = await conn.execute(
                "SELECT id, from_account, to_account, amount, currency, reference, status, "
                "reason, created_at, posted_at FROM transactions WHERE id = %s",
                (txn_id,),
            )
            row = await cur.fetchone()
        if row is not None and row["status"] != "pending":
            row["id"] = str(row["id"])
            return row
        cached = await request.app.state.redis.hgetall(f"txn:{txn_id}")
        if cached:
            return {"id": str(txn_id), "status": cached["status"], "reason": cached.get("reason") or None}
        raise HTTPException(404, "transaction not found")

    @app.get("/v1/ledger/integrity")
    async def integrity(request: Request, _: str = Depends(authenticate)):
        """Double-entry invariant: total debits == total credits and balances sum to zero."""
        async with request.app.state.ro_pool.connection() as conn:
            cur = await conn.execute(
                "SELECT COALESCE(SUM(amount) FILTER (WHERE direction = 'debit'), 0) AS debits, "
                "COALESCE(SUM(amount) FILTER (WHERE direction = 'credit'), 0) AS credits "
                "FROM ledger_entries"
            )
            totals = await cur.fetchone()
            cur = await conn.execute("SELECT COALESCE(SUM(balance), 0) AS total FROM accounts")
            balances = await cur.fetchone()
        debits, credits, total = int(totals["debits"]), int(totals["credits"]), int(balances["total"])
        return {
            "total_debits": debits,
            "total_credits": credits,
            "sum_of_balances": total,
            "consistent": debits == credits and total == 0,
        }

    return app


app = create_app()

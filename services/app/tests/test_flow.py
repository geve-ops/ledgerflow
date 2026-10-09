import asyncio
import uuid

from conftest import drain, new_id

CURRENCY = "USD"


async def make_account(client, allow_negative=False, currency=CURRENCY):
    acct = new_id()
    r = await client.post(
        "/v1/accounts", json={"id": acct, "currency": currency, "allow_negative": allow_negative}
    )
    assert r.status_code == 201, r.text
    return acct


async def transfer(client, src, dst, amount, key=None, currency=CURRENCY):
    return await client.post(
        "/v1/transactions",
        json={"from_account": src, "to_account": dst, "amount": amount, "currency": currency},
        headers={"Idempotency-Key": key or uuid.uuid4().hex},
    )


async def balance(client, acct):
    return (await client.get(f"/v1/accounts/{acct}")).json()["balance"]


async def test_health_and_auth(client):
    assert (await client.get("/healthz")).status_code == 200
    assert (await client.get("/readyz")).status_code == 200
    r = await client.get("/v1/accounts/x", headers={"X-API-Key": "wrong"})
    assert r.status_code == 401


async def test_autoscaling_counter_exists_before_any_traffic(client):
    # The HPA metric must be present on idle pods; a labelled counter would be absent.
    text = (await client.get("/metrics")).text
    assert "ledger_api_requests_total " in text
    before = float(text.split("ledger_api_requests_total ")[1].split()[0])
    await client.get("/v1/accounts/nope")
    after_text = (await client.get("/metrics")).text
    after = float(after_text.split("ledger_api_requests_total ")[1].split()[0])
    assert after == before + 1  # counted; /metrics and probes are not


async def test_end_to_end_posting(client, worker):
    treasury = await make_account(client, allow_negative=True)
    alice = await make_account(client)

    r = await transfer(client, treasury, alice, 5_000)
    assert r.status_code == 202
    txn_id = r.json()["transaction_id"]
    assert (await client.get(f"/v1/transactions/{txn_id}")).json()["status"] == "pending"

    await drain(worker)
    body = (await client.get(f"/v1/transactions/{txn_id}")).json()
    assert body["status"] == "posted"
    assert await balance(client, alice) == 5_000
    assert await balance(client, treasury) == -5_000


async def test_idempotency_replay_and_conflict(client, worker):
    treasury = await make_account(client, allow_negative=True)
    alice = await make_account(client)
    key = uuid.uuid4().hex

    first = await transfer(client, treasury, alice, 100, key=key)
    again = await transfer(client, treasury, alice, 100, key=key)
    assert first.status_code == 202 and again.status_code == 200
    assert again.json() == {"transaction_id": first.json()["transaction_id"], "replayed": True}

    conflict = await transfer(client, treasury, alice, 999, key=key)
    assert conflict.status_code == 409

    await drain(worker)
    assert await balance(client, alice) == 100  # posted exactly once


async def test_missing_idempotency_key_rejected(client):
    r = await client.post(
        "/v1/transactions",
        json={"from_account": "a", "to_account": "b", "amount": 1, "currency": "USD"},
    )
    assert r.status_code == 400


async def test_business_rejections(client, worker):
    treasury = await make_account(client, allow_negative=True)
    alice = await make_account(client)
    bob = await make_account(client)
    eur = await make_account(client, currency="EUR")

    cases = {
        "insufficient_funds": await transfer(client, alice, bob, 1),
        "unknown_account": await transfer(client, treasury, "ghost-account", 1),
        "currency_mismatch": await transfer(client, treasury, eur, 1),
    }
    await drain(worker)
    for reason, r in cases.items():
        body = (await client.get(f"/v1/transactions/{r.json()['transaction_id']}")).json()
        assert body["status"] == "rejected" and body["reason"] == reason


async def test_concurrent_transfers_conserve_money(client, worker):
    treasury = await make_account(client, allow_negative=True)
    accounts = [await make_account(client) for _ in range(4)]
    for a in accounts:
        await transfer(client, treasury, a, 10_000)
    await drain(worker, attempts=8)

    # Criss-cross transfers between accounts, processed by two competing consumers.
    from ledgerflow.worker.main import Worker

    worker2 = Worker(worker.s, worker.pool, worker.redis, consumer="test-consumer-2")
    for i in range(40):
        src, dst = accounts[i % 4], accounts[(i + 1) % 4]
        await transfer(client, src, dst, 7)
    await asyncio.gather(drain(worker, 8), drain(worker2, 8))

    assert sum([await balance(client, a) for a in accounts]) == 40_000
    integrity = (await client.get("/v1/ledger/integrity")).json()
    assert integrity["consistent"] is True


async def test_rate_limit(settings, pool):
    import httpx

    from ledgerflow.api.main import create_app

    app = create_app(settings.model_copy(update={"rate_limit_per_minute": 3}))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"X-API-Key": "other-key"},
        ) as c:
            codes = [(await c.get("/v1/accounts/nope")).status_code for _ in range(5)]
    assert codes == [404, 404, 404, 429, 429]


async def test_audit_log_is_append_only(client, worker, pool):
    import psycopg

    treasury = await make_account(client, allow_negative=True)
    alice = await make_account(client)
    await transfer(client, treasury, alice, 1)
    await drain(worker)

    async with pool.connection() as conn:
        try:
            await conn.execute("UPDATE audit_log SET actor = 'evil'")
            raised = False
        except psycopg.errors.RaiseException:
            raised = True
    assert raised


async def test_poison_message_goes_to_dlq(settings, worker, redis):
    await redis.xadd(settings.stream, {"data": "{not json"})
    await drain(worker)
    assert await redis.xlen(settings.dlq_stream) == 1
    pending = await redis.xpending(settings.stream, settings.group)
    assert pending["pending"] == 0

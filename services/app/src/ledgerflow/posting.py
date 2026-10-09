"""Double-entry posting. Runs inside a single Postgres transaction."""
import json

from psycopg_pool import AsyncConnectionPool

POSTED = "posted"
REJECTED = "rejected"
DUPLICATE = "duplicate"


async def post_transaction(pool: AsyncConnectionPool, ev: dict) -> tuple[str, str | None]:
    """Post one event. Returns (status, reason).

    Idempotent: the INSERT ... ON CONFLICT DO NOTHING on the transaction id (and on
    client_id + idempotency_key) means a redelivered event is detected and skipped.
    Accounts are locked in sorted order so concurrent postings cannot deadlock.
    """
    async with pool.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute(
                """
                INSERT INTO transactions
                    (id, idempotency_key, client_id, from_account, to_account,
                     amount, currency, reference, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'pending')
                ON CONFLICT DO NOTHING
                RETURNING id
                """,
                (
                    ev["id"], ev["idempotency_key"], ev["client_id"], ev["from_account"],
                    ev["to_account"], ev["amount"], ev["currency"], ev.get("reference"),
                ),
            )
            if await cur.fetchone() is None:
                return DUPLICATE, None

            ids = sorted({ev["from_account"], ev["to_account"]})
            cur = await conn.execute(
                "SELECT id, currency, balance, allow_negative FROM accounts "
                "WHERE id = ANY(%s) ORDER BY id FOR UPDATE",
                (ids,),
            )
            accounts = {row["id"]: row for row in await cur.fetchall()}

            reason = _validate(ev, accounts)
            if reason:
                await conn.execute(
                    "UPDATE transactions SET status = 'rejected', reason = %s WHERE id = %s",
                    (reason, ev["id"]),
                )
                await _audit(conn, ev, "transaction.rejected", {"reason": reason})
                return REJECTED, reason

            amount = ev["amount"]
            await conn.execute(
                "UPDATE accounts SET balance = balance - %s WHERE id = %s",
                (amount, ev["from_account"]),
            )
            await conn.execute(
                "UPDATE accounts SET balance = balance + %s WHERE id = %s",
                (amount, ev["to_account"]),
            )
            await conn.execute(
                """
                INSERT INTO ledger_entries (transaction_id, account_id, direction, amount)
                VALUES (%s, %s, 'debit', %s), (%s, %s, 'credit', %s)
                """,
                (ev["id"], ev["from_account"], amount, ev["id"], ev["to_account"], amount),
            )
            await conn.execute(
                "UPDATE transactions SET status = 'posted', posted_at = now() WHERE id = %s",
                (ev["id"],),
            )
            await _audit(conn, ev, "transaction.posted", {"amount": amount})
            return POSTED, None


def _validate(ev: dict, accounts: dict) -> str | None:
    src = accounts.get(ev["from_account"])
    dst = accounts.get(ev["to_account"])
    if src is None or dst is None:
        return "unknown_account"
    if src["currency"] != ev["currency"] or dst["currency"] != ev["currency"]:
        return "currency_mismatch"
    if not src["allow_negative"] and src["balance"] < ev["amount"]:
        return "insufficient_funds"
    return None


async def _audit(conn, ev: dict, action: str, details: dict) -> None:
    await conn.execute(
        "INSERT INTO audit_log (actor, action, entity, entity_id, details) "
        "VALUES (%s, %s, 'transaction', %s, %s::jsonb)",
        (ev["client_id"], action, ev["id"], json.dumps(details)),
    )

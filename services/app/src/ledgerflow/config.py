from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LEDGER_")

    database_url: str = "postgresql://ledger:ledger@localhost:5432/ledger"
    # Optional read-replica endpoint (CNPG "-ro" service); falls back to database_url.
    database_ro_url: str | None = None
    redis_url: str = "redis://localhost:6379/0"
    redis_password: str | None = None

    # "key:client_id" pairs, comma separated. Supplied from a SealedSecret in-cluster.
    api_keys: str = "dev-key:dev-client"
    rate_limit_per_minute: int = 120

    stream: str = "ledger:events"
    dlq_stream: str = "ledger:events:dlq"
    group: str = "ledger-workers"
    max_deliveries: int = 5
    claim_idle_ms: int = 30_000
    batch_size: int = 50
    concurrency: int = 8   # in-flight postings per worker; keep <= db_pool_max
    idempotency_ttl_seconds: int = 86_400
    txn_status_ttl_seconds: int = 86_400

    db_pool_min: int = 1
    db_pool_max: int = 10
    worker_metrics_port: int = 9100

    def api_key_map(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for pair in self.api_keys.split(","):
            if ":" in pair:
                key, client = pair.strip().split(":", 1)
                out[key] = client
        return out

from prometheus_client import Counter, Gauge, Histogram

HTTP_REQUESTS = Counter(
    "ledger_http_requests_total", "HTTP requests", ["method", "route", "status"]
)
HTTP_LATENCY = Histogram(
    "ledger_http_request_duration_seconds",
    "HTTP request latency",
    ["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5),
)
# Label-free on purpose: an unlabelled counter is exported as 0 from process start, so every
# pod always has this series. The HPA metric is built on it; a labelled counter only appears
# after the first request, which would make idle pods look like "metric missing" to the HPA.
API_REQUESTS = Counter(
    "ledger_api_requests_total", "API requests served (probes and /metrics excluded)"
)
SUBMITTED = Counter(
    "ledger_transactions_submitted_total", "Transactions accepted by the API", ["outcome"]
)
RATE_LIMITED = Counter(
    "ledger_rate_limited_total", "Requests rejected by the rate limiter", ["client"]
)
IDEMPOTENT_REPLAYS = Counter(
    "ledger_idempotent_replays_total", "Requests served from an existing idempotency key"
)
PROCESSED = Counter(
    "ledger_transactions_total", "Transactions processed by the worker", ["status"]
)
POSTING_LATENCY = Histogram(
    "ledger_posting_duration_seconds",
    "Time to post one transaction to Postgres",
    buckets=(0.002, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1),
)
STREAM_LAG = Gauge("ledger_stream_lag", "Entries not yet delivered to the consumer group")
STREAM_PENDING = Gauge("ledger_stream_pending", "Entries delivered but not yet acknowledged")

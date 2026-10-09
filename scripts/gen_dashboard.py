"""Generates gitops/observability/dashboard.yaml (a Grafana dashboard ConfigMap).

    python scripts/gen_dashboard.py
"""
import json
import pathlib

DS = {"type": "prometheus", "uid": "prometheus"}
panels = []
_id = 0


def panel(title, targets, unit="short", kind="timeseries", x=0, y=0, w=8, h=8, stack=False):
    global _id
    _id += 1
    p = {
        "id": _id,
        "title": title,
        "type": kind,
        "datasource": DS,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [
            {"refId": chr(65 + i), "expr": e, "legendFormat": legend, "datasource": DS}
            for i, (e, legend) in enumerate(targets)
        ],
        "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
    }
    if kind == "timeseries":
        p["fieldConfig"]["defaults"]["custom"] = {
            "lineWidth": 2,
            "fillOpacity": 15,
            "stacking": {"mode": "normal" if stack else "none"},
        }
    panels.append(p)


# Row 1: traffic and correctness
panel("Transactions posted / s", [('sum(rate(ledger_transactions_total{status="posted"}[1m]))', "posted")], "ops", x=0, y=0, w=6)
panel("Accepted by API / s", [("sum by (outcome) (rate(ledger_transactions_submitted_total[1m]))", "{{outcome}}")], "ops", x=6, y=0, w=6, stack=True)
panel(
    "5xx error ratio",
    [('sum(rate(ledger_http_requests_total{status=~"5.."}[5m])) / clamp_min(sum(rate(ledger_http_requests_total[5m])), 1e-9)', "5xx ratio")],
    "percentunit", x=12, y=0, w=6,
)
panel("Business rejections / s", [('sum(rate(ledger_transactions_total{status=~"rejected|dead_lettered|error"}[1m])) by (status)', "{{status}}")], "ops", x=18, y=0, w=6)

# Row 2: latency
q = lambda p: f"histogram_quantile({p}, sum by (le) (rate(ledger_http_request_duration_seconds_bucket[1m])))"
panel("API latency (p50 / p95 / p99)", [(q(0.5), "p50"), (q(0.95), "p95"), (q(0.99), "p99")], "s", x=0, y=8, w=12)
pq = lambda p: f"histogram_quantile({p}, sum by (le) (rate(ledger_posting_duration_seconds_bucket[1m])))"
panel("Ledger posting time (p50 / p99)", [(pq(0.5), "p50"), (pq(0.99), "p99")], "s", x=12, y=8, w=12)

# Row 3: pipeline and scaling
panel("Event stream: lag and pending", [("max(ledger_stream_lag)", "not yet delivered"), ("max(ledger_stream_pending)", "unacknowledged")], x=0, y=16, w=8)
panel(
    "Replicas (HPA)",
    [
        ('kube_deployment_status_replicas_available{namespace="ledger",deployment="ledgerflow-api"}', "api"),
        ('kube_deployment_status_replicas_available{namespace="ledger",deployment="ledgerflow-worker"}', "worker"),
    ],
    x=8, y=16, w=8,
)
panel("API requests / s per pod", [('sum by (pod) (rate(ledger_http_requests_total{namespace="ledger"}[1m]))', "{{pod}}")], "reqps", x=16, y=16, w=8)

# Row 4: data layer
panel("Rate-limited requests / s", [("sum by (client) (rate(ledger_rate_limited_total[1m]))", "{{client}}")], "ops", x=0, y=24, w=6)
panel("Idempotent replays / s", [("sum(rate(ledger_idempotent_replays_total[1m]))", "replays")], "ops", x=6, y=24, w=6)
panel("Postgres replication lag", [("max by (pod) (cnpg_pg_replication_lag)", "{{pod}}")], "s", x=12, y=24, w=6)
panel("Redis memory", [("redis_memory_used_bytes", "used"), ("redis_memory_max_bytes", "limit")], "bytes", x=18, y=24, w=6)

dashboard = {
    "uid": "ledgerflow-overview",
    "title": "Ledgerflow: transactions, latency and scaling",
    "tags": ["ledgerflow"],
    "timezone": "browser",
    "schemaVersion": 39,
    "version": 1,
    "refresh": "10s",
    "time": {"from": "now-30m", "to": "now"},
    "panels": panels,
}

out = pathlib.Path(__file__).resolve().parent.parent / "gitops" / "observability" / "dashboard.yaml"
body = json.dumps(dashboard, indent=2)
indented = "\n".join("    " + line for line in body.splitlines())
out.write_text(
    "apiVersion: v1\n"
    "kind: ConfigMap\n"
    "metadata:\n"
    "  name: ledgerflow-dashboard\n"
    "  namespace: monitoring\n"
    "  labels:\n"
    '    grafana_dashboard: "1"\n'
    "data:\n"
    "  ledgerflow.json: |\n" + indented + "\n"
)
print("wrote", out)

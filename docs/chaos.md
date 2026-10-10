# Failure experiments

Each experiment runs a steady 40 req/s of payment submissions (k6, inside the cluster, through
the TLS gateway) while one thing is broken on purpose. Afterwards the ledger is audited:

- events accepted = `XLEN ledger:events` in Redis (the stream is never trimmed)
- payments posted = `count(*)` of `transactions` with status `posted` in Postgres
- dead letters = `XLEN ledger:events:dlq`

These two counts must be equal. Debits must equal credits, and balances must sum to zero.

All runs are on one laptop (3 Kind nodes), so timings show behaviour, not production numbers.

## 1. Kill the Postgres primary

```powershell
./scripts/chaos-failover.ps1
```

The primary pod is force-deleted 60 s into a 3-minute run.

| | |
|---|---|
| Replica promoted | after **25 s** |
| Cluster back to 3 healthy instances | after **49.5 s** |
| Payment submissions | all accepted (the API only needs Redis to accept a payment) |
| Failed requests | 4 of 7,967, all read-backs (`GET /v1/transactions/{id}`) during the switch |
| Result | 173,147 accepted = 173,147 posted, 0 dead-lettered, debits = credits |

While the primary was gone the workers could not post, so events queued in the stream and were
posted once the new primary was up.

## 2. Roll out a change through Git

```powershell
./scripts/chaos-rollout.ps1
```

A rate-limit value is changed in `values-gitops.yaml` and pushed. Argo CD detects it and rolls
the API (`maxUnavailable: 0`, `maxSurge: 1`).

| | |
|---|---|
| Push to rollout complete | **33 s** (rollout began after 14 s) |
| Ready API replicas during rollout | never below the desired count |
| Failed requests | **0 of 6,602** |
| Result | 179,168 accepted = 179,168 posted |

## 3. Drain a node

```powershell
./scripts/chaos-drain.ps1            # drains ledgerflow-worker2
```

The node hosts API pods and two Postgres replicas. The load generator is pinned to the other
worker so the drain cannot evict it. The drain is given 100 s, then the node is uncordoned.

| | |
|---|---|
| Stateless pods | evicted and rescheduled onto the other node |
| Postgres | one replica evicted; the PodDisruptionBudget **refused to evict the second** ("would violate the pod's disruption budget") until the timeout, so a primary and a replica always stayed up |
| Failed requests | 1 of 7,977 (a read-back); every submission accepted; p99 10 ms |
| After uncordon | cluster back to 3 healthy instances in **88 s** |
| Result | 187,904 accepted = 187,904 posted, 0 dead-lettered |

Local volumes are bound to their node, so an evicted replica cannot start elsewhere; with
network-attached storage it would reschedule immediately.

**A note on an earlier attempt.** The first drain run evicted the k6 pod itself (it had landed
on the drained node), which ended the load test, so it produced no client-side numbers. The
runner now pins the load generator to a node that is not drained, and the chaos scripts abort
if the load generator does not start. A second attempt then generated no load because of a
script error; that is why the runs were repeated. The table above is from the valid run.

## 4. Load beyond the worker's capacity

```powershell
./scripts/loadtest.ps1 -PeakRps 300
```

Same traffic, before and after making the worker concurrent and autoscaling it on queue depth.

| | before | after |
|---|---|---|
| Peak API rate | 332 req/s | 332 req/s |
| Peak posting rate | 144 tx/s | 270 tx/s |
| Peak backlog (events waiting) | 41,618 | 9,645 |
| Workers | fixed at 2 | 2 -> 6 on queue depth |
| Failed requests | 0 | 1 of 91,331 (plus 21 expected 409s from re-creating accounts, now excluded) |

In both runs nothing was lost: the stream absorbed the difference between how fast payments were
accepted and how fast they could be posted, and every event was posted exactly once.

# Operations

Deployment manifests, sizing, autoscaling, scaling, disaster recovery and troubleshooting.
Related: [architecture.md](architecture.md) | [security.md](security.md) |
[observability.md](observability.md) | [backups.md](backups.md) | [chaos.md](chaos.md)

Each procedure says whether it was **tested** here, **partly tested**, or **not tested**. Commands
are PowerShell/`kubectl`; the cluster context is `kind-ledgerflow`.

---

## 6. Infrastructure as code and sizing

### 6.1 Where the manifests are

Everything deployed is in Git and reconciled by Argo CD (`gitops/apps/*.yaml`, 16 Applications).
The complete, current YAML for every object is in
**[reference/manifests.md](reference/manifests.md)**, which is generated from the repository and
the rendered Helm chart so it cannot drift (`python scripts/gen_manifest_reference.py`).

| Concern | Source of truth |
|---|---|
| API, worker, migration hook, HPAs, PDBs, ServiceAccounts | `charts/ledgerflow/` (Helm), values in `values.yaml` + `values-gitops.yaml` |
| Postgres, Redis, secrets | `gitops/data/` |
| Gateway, TLS | `gitops/edge/` |
| NetworkPolicies | `gitops/network-policies/` |
| Backups | `gitops/backup/`, `gitops/platform/barman-plugin/` |
| Monitoring and alerts | `gitops/observability/`, `gitops/platform/*-values.yaml` |
| Cluster and CNI bootstrap | `cluster/`, `scripts/bootstrap.ps1` |

Sync waves order the install: namespaces (-10) -> Sealed Secrets, CNPG operator, Envoy Gateway
(-9) -> cert-manager (-8; needs the Gateway API CRDs) -> Barman plugin (-7) -> backup store (-6)
-> edge and data (0) -> network policies (1) -> application (2) -> monitoring (3-4).
Cross-application ordering is only eventual, so a first install shows transient `Degraded` states
that retries resolve.

### 6.2 Requests and limits

Memory limits are set everywhere; **CPU limits are deliberately omitted** (CFS throttling adds
latency spikes, and CPU requests drive scheduling and the HPA's utilisation maths). Requests
below are the values deployed.

| Component | CPU request | Memory request | Memory limit | Notes |
|---|---|---|---|---|
| `ledgerflow-api` | 100m | 128Mi | 256Mi | HPA 2 to 6 |
| `ledgerflow-worker` | 100m | 128Mi | 256Mi | HPA 2 to 6 |
| `ledgerflow-migrate` (Job) | 50m | 64Mi | 128Mi | |
| PostgreSQL (each of 3) | 100m | 256Mi | 512Mi | `shared_buffers` 128MB, `max_connections` 100 |
| Barman sidecar (in each Postgres pod) | **none** | **none** | **none** | Gap: add requests/limits (the pod is then Burstable) |
| Redis | 50m | 128Mi | 384Mi | `maxmemory 256mb` |
| Redis exporter | 10m | 24Mi | 64Mi | |
| SeaweedFS | 50m | 256Mi | 768Mi | |
| Envoy proxy | 50m | 64Mi | 256Mi | |
| Prometheus | 200m | 600Mi | 1500Mi | 3 d retention |
| Grafana | 50m | 128Mi | 384Mi | |
| Argo CD controller / server / repo-server | 100m / 50m / 50m | 256Mi / 128Mi / 128Mi | 768Mi / 256Mi / 512Mi | |
| Other platform pods | 25m to 50m | 48Mi to 128Mi | 128Mi to 256Mi | See [manifests](reference/manifests.md#platform-helm-values) |

Connection budget (a real constraint): Postgres allows 100 connections. API pods use up to 5 each
and worker pods up to 8 each (`LEDGER_DB_POOL_MAX`), so the maximum is `6 x 5 + 6 x 8 = 78`,
leaving headroom for the migration Job, replication and the operator. If `maxReplicas` is raised,
raise `max_connections` or put a pooler in front.

### 6.3 Sizing guidance derived from measurements

These are extrapolations from a laptop test and **not validated at scale**.

| Observation (measured) | Implication |
|---|---|
| API pods used about 380m CPU each at roughly 55 req/s per pod (about 7 millicores per request/s) | About 140 req/s per CPU core for this workload |
| Worker throughput peaked at 270 posted tx/s with 6 workers x 8 concurrency | The single Postgres primary and hot-row locking become the limit before the worker count does |
| First run: API accepted 332 req/s while 2 sequential workers posted 144 tx/s | Intake scales far more cheaply than posting; size and alert on the **queue**, not the API |

Starting points for a real environment (to be load-tested, not guaranteed):

| Component | Request | Limit (memory) |
|---|---|---|
| API | 250m / 256Mi | 512Mi |
| Worker | 250m / 256Mi | 512Mi |
| PostgreSQL | 2 CPU / 4Gi (`shared_buffers` 1GB) | 4Gi |
| Redis | 500m / 1Gi (`maxmemory` 768mb) | 1.25Gi |

### 6.4 Horizontal Pod Autoscalers (implemented)

```yaml
# ledgerflow-api: CPU and per-pod request rate (custom metric via prometheus-adapter)
minReplicas: 2
maxReplicas: 6
metrics:
  - type: Resource
    resource: {name: cpu, target: {type: Utilization, averageUtilization: 70}}
  - type: Pods
    pods:
      metric: {name: ledger_api_requests_per_second}
      target: {type: AverageValue, averageValue: "40"}
behavior:
  scaleUp:   {stabilizationWindowSeconds: 0,   policies: [{type: Pods, value: 2, periodSeconds: 30}]}
  scaleDown: {stabilizationWindowSeconds: 120}
```

```yaml
# ledgerflow-worker: queue depth (external metric via prometheus-adapter)
minReplicas: 2
maxReplicas: 6
metrics:
  - type: External
    external:
      metric: {name: ledger_stream_lag}
      target: {type: AverageValue, averageValue: "500"}   # desired = ceil(lag / 500)
behavior:
  scaleUp:   {stabilizationWindowSeconds: 0,   policies: [{type: Pods, value: 2, periodSeconds: 30}]}
  scaleDown: {stabilizationWindowSeconds: 180}
```

| HPA | Trigger | Measured behaviour |
|---|---|---|
| API | CPU > 70 % of request **or** > 40 req/s per pod | 2 -> 6 pods in about 70 s under a 300 req/s ramp; stayed at 6 while load persisted |
| Worker | > 500 queued events per pod | 2 -> 4 -> 6 pods during the ramp; backlog drained about 40 s after load ended; scaled back 6 -> 4 -> 2 afterwards |

Metric plumbing: `metrics-server` serves CPU (`metrics.k8s.io`); `prometheus-adapter` serves
`custom.metrics.k8s.io` (rate of `ledger_api_requests_total` per pod) and
`external.metrics.k8s.io` (`ledger_stream_lag`, max across pods). `kubectl get hpa` showing
`<unknown>` means the adapter or Prometheus is down, or the metric series is missing (see
[Other issues](#other-issues-seen-during-this-project)).

### 6.5 Vertical Pod Autoscaler (not implemented)

No VPA is installed. Two cautions before adding one:

- **Do not let VPA act on CPU for the API or worker** while an HPA scales them on CPU: the two
  controllers fight (VPA changes the request, which changes the utilisation the HPA divides by).
- VPA's updater evicts pods to apply new requests, which interacts with the local, node-pinned
  volumes for Postgres and Redis.

The safe use is **recommendation-only** (`updateMode: "Off"`), reading the suggested requests to
tune the values in section 6.2. Proposed, after installing the VPA components (recommender at
minimum):

```yaml
apiVersion: autoscaling.k8s.io/v1
kind: VerticalPodAutoscaler
metadata: {name: ledgerflow-worker, namespace: ledger}
spec:
  targetRef: {apiVersion: apps/v1, kind: Deployment, name: ledgerflow-worker}
  updatePolicy: {updateMode: "Off"}
  resourcePolicy:
    containerPolicies:
      - containerName: worker
        minAllowed: {cpu: 50m, memory: 64Mi}
        maxAllowed: {cpu: "1", memory: 1Gi}
---
apiVersion: autoscaling.k8s.io/v1
kind: VerticalPodAutoscaler
metadata: {name: redis, namespace: ledger}
spec:
  targetRef: {apiVersion: apps/v1, kind: StatefulSet, name: redis}
  updatePolicy: {updateMode: "Off"}   # recommendations only: eviction would interrupt ingestion
```

### 6.6 Manual scaling runbook

| Goal | How | Notes |
|---|---|---|
| More API/worker capacity | Raise `maxReplicas` (or `minReplicas`) in `charts/ledgerflow/values-gitops.yaml`, commit, push | Check the connection budget (6.2) first |
| Pause intake | Disable the API HPA and set `api.replicas: 0` in values (commit) | Existing events still drain; workers keep running |
| More Postgres replicas | `spec.instances` in `gitops/data/cnpg-cluster.yaml` | Each replica needs a node and a 2Gi volume; with 2 workers, 3 is the practical maximum on this cluster |
| More Redis memory | `maxmemory` in `redis-config` and the container memory limit (keep limit > maxmemory) | Single instance: a restart pauses intake |
| Find the bottleneck | Dashboard: lag rising = worker/DB bound; request latency rising = API bound; `cnpg_backends` near 100 = connection bound | The observed order was worker, then database, then API |

Never `kubectl scale` a Deployment owned by an HPA or Argo CD: the change is reverted.

---

## 7. Disaster recovery

### 7.1 Objectives and what they depend on

| Scenario | Data loss (RPO) | Time to recover (RTO) | Status |
|---|---|---|---|
| API/worker pod crash | none | seconds | tested |
| Postgres primary killed | none (replicas are current) | **25 s** to promote, **50 s** to 3/3 | **tested** |
| Node drained | none | **88 s** after uncordon for Postgres to be 3/3 | **tested** |
| Redis restart | up to ~1 s of acknowledged writes (AOF `everysec`) | seconds; intake returns `503` meanwhile | **not tested** (expected from the configuration) |
| Database lost, backups intact | **up to 5 min** of WAL (`archive_timeout` = 5 min) if the whole cluster is lost | minutes to restore at this data size (not timed) | **restore tested** (point in time, separate cluster) |
| Whole cluster lost, backups were in it | **everything** | n/a | **gap**: backups live in the cluster (see below) |
| Hard node crash | none expected | pod eviction defaults to 5 min | **not tested** |

> **The backup target is a pod in the same cluster.** If the cluster or its Docker volumes are
> destroyed, the backups go with it. Before relying on this for recovery, point the `ObjectStore`
> (`gitops/backup/objectstore.yaml`) at a bucket **outside** the failure domain (another account or
> region) and re-seal its credentials. Everything below assumes that has been done.

### 7.2 What can be lost in an outage (and why it is safe)

- **Events accepted but not yet posted** live in Redis. If the Redis volume is lost they are gone,
  but the client received only a `202`, never a "posted" status. Clients that resubmit with the
  same `Idempotency-Key` are safe even though the Redis idempotency keys are gone, because
  PostgreSQL enforces `UNIQUE (client_id, idempotency_key)`: a duplicate is detected at posting.
- **Posted data** is in PostgreSQL and in the backups; it is the only thing that must survive.
- **The audit log and ledger entries** are append-only and are restored with the database.

### 7.3 Runbook A: a single failed component (automatic)

| Failure | Expected | Verify |
|---|---|---|
| API or worker pod | Restarted/rescheduled; PDB and replicas keep service | `kubectl -n ledger get pods`; dashboard error ratio |
| Postgres primary | Operator promotes a replica, repoints `ledger-db-rw`, rebuilds the old primary as a replica | `kubectl -n ledger get cluster ledger-db` shows `Cluster in healthy state`, new `primary` |
| Postgres replica | Operator recreates it and re-clones | same |

To force a failover as a test: `kubectl -n ledger delete pod <primary> --force --grace-period=0`
(the `kubectl cnpg` plugin is not installed). This is `scripts/chaos-failover.ps1`.

After any failover, run the integrity check and compare the event and posted counts:

```powershell
$p = kubectl -n ledger get pod -l cnpg.io/instanceRole=primary -o jsonpath='{.items[0].metadata.name}'
kubectl -n ledger exec redis-0 -c redis -- redis-cli XLEN ledger:events          # accepted
kubectl -n ledger exec $p -c postgres -- psql -U postgres -d ledger -t -A -c "select count(*) from transactions where status in ('posted','rejected');"   # processed
kubectl -n ledger exec redis-0 -c redis -- redis-cli XLEN ledger:events:dlq       # must be 0
```

The two counts must be equal once the backlog drains.

### 7.4 Runbook B: restore the database (partly tested)

**B1. Restore into a separate, throwaway cluster to verify a backup or find a point in time.
Tested.**

```powershell
./scripts/restore-test.ps1 -TargetTime '2026-10-10 09:10:29.550108+00'   # omit -TargetTime for "latest"
kubectl -n restore-test get cluster ledger-restored                       # wait for healthy
# compare with the expected state, then:
./scripts/restore-test.ps1 -Cleanup
```

**B2. Recover the production database from a backup after total loss. The recovery mechanism is
tested (B1); doing it as the bootstrap of the production-named cluster on a rebuilt platform has
not been run end to end.**

1. Make sure the off-cluster `ObjectStore` and its sealed credentials are correct in
   `gitops/backup/` (see the warning in 7.1).
2. **Before** creating the new cluster, edit `gitops/data/cnpg-cluster.yaml` and push. Replace the
   `initdb` bootstrap with recovery from the object store:

   ```yaml
   spec:
     bootstrap:
       recovery:
         source: source
         # recoveryTarget:
         #   targetTime: "2026-10-10 09:10:29.550108+00"   # optional point in time
     externalClusters:
       - name: source
         plugin:
           name: barman-cloud.cloudnative-pg.io
           parameters:
             barmanObjectName: ledger-store
             serverName: ledger-db          # the name of the cluster that wrote the backups
   ```

   This must be in Git first, because Argo CD applies the data layer as soon as the cluster
   exists and an `initdb` would create an empty database. `bootstrap` is only read at first
   creation, so it can stay in place afterwards.
3. Rebuild the platform (Runbook C), which creates the cluster from the recovery spec.
4. Verify (section 7.7).

### 7.5 Runbook C: rebuild the cluster and migrate (partly tested)

Prerequisites: the repository, the **Sealed Secrets key backup**
(`.local/sealed-secrets-key.json`, refreshed after each key rotation, see
[security.md](security.md#33-key-custody-and-rotation-important)), and the database backups
reachable from the new cluster.

| # | Step | Command / action |
|---|---|---|
| 1 | Recreate the cluster, CNI, key and Argo CD | `./scripts/bootstrap.ps1` (**tested** before monitoring and backups were added; **not re-run** since) |
| 2 | Wait for the platform | `kubectl -n argocd get applications -w`; expect transient `Degraded` while operators start; all 16 `Synced`/`Healthy` |
| 3 | Confirm secrets decrypted | `kubectl -n ledger get secret ledger-secrets s3-credentials`; if missing, the key backup was not restored |
| 4 | Confirm the database | `kubectl -n ledger get cluster ledger-db`; run section 7.7 |
| 5 | Re-trust the local CA (local setup only) | `kubectl -n cert-manager get secret ledgerflow-root-ca -o jsonpath='{.data.ca\.crt}'` |
| 6 | Reach the edge | `./scripts/edge-forward.ps1` |

**Planned migration to a new cluster** (no data loss):

1. Stop intake: disable the API HPA and set `api.replicas: 0` in `values-gitops.yaml`; commit.
2. Wait for the queue to drain: `ledger_stream_lag` and `ledger_stream_pending` both 0.
3. Take a final backup and force the last WAL segment out:
   `kubectl apply` a `Backup` (`method: plugin`), then `select pg_switch_wal();` on the primary.
4. Build the new cluster with B2 (recover to latest).
5. Verify (7.7), then move traffic (DNS/route) and decommission the old cluster.
6. Re-enable the API in Git.

Expected downtime is the time between steps 1 and 5. The sealed secrets work on the new cluster
only if the sealing key was copied (step 1 of the table).

### 7.6 Scenario: whole region/disk lost, nothing else damaged

Same as 7.5 + B2, provided the backups are off-cluster. In the one full rebuild that was run
(before monitoring and backups were added), the bootstrap script took about 15 minutes and Argo CD
needed about another 15 to reach all-healthy, so **budget about 30 minutes for the platform**; the
slowest part was the first database creation (image pulls and initialisation took about 9
minutes). Add the restore time on top.

### 7.7 Post-recovery verification checklist

```powershell
# 1. Every Argo CD application Synced and Healthy
kubectl -n argocd get applications
# 2. Database healthy, one primary, 2 streaming replicas
kubectl -n ledger get cluster ledger-db
# 3. Ledger invariants on the primary
kubectl -n ledger exec <primary> -c postgres -- psql -U postgres -d ledger -t -A -c "select ((select sum(amount) from ledger_entries where direction='debit') = (select sum(amount) from ledger_entries where direction='credit')) and ((select sum(balance) from accounts) = 0);"
# 4. Row counts equal the last known good state (transactions, accounts, audit_log)
# 5. API: GET /v1/ledger/integrity -> {"consistent": true}
# 6. Archiving working: kubectl -n ledger get cluster ledger-db -o jsonpath='{.status.conditions}' (ContinuousArchiving=True)
# 7. No firing alerts other than Watchdog
```

---

## 8. Troubleshooting: top 5 failure scenarios

Each entry gives the symptom, how to confirm, the project-specific causes, the fix and the
prevention. Where it happened during this project, that is stated.

### 1. `CrashLoopBackOff`

**Seen here:** after rebuilding the cluster, the API pods restarted 3 times before the database
existed (first Postgres creation took about 9 minutes).

```powershell
kubectl -n ledger get pods
kubectl -n ledger describe pod <pod>                 # Last State, Exit Code, Events
kubectl -n ledger logs <pod> --previous              # why the last container died
```

| Cause | Evidence | Fix |
|---|---|---|
| Database or Redis unavailable at start | `PoolTimeout: pool initialization incomplete` in the API log | Wait; the pods recover once the database is up. Check `kubectl -n ledger get cluster ledger-db` |
| Missing or wrong secret | `CreateContainerConfigError`, or auth errors | `kubectl -n ledger get secret ledger-secrets ledger-db-app`; check Sealed Secrets decrypted |
| Bad image or command after a deploy | Exit code 1 immediately, import errors | Roll back in Git (revert the tag bump); Argo CD redeploys |
| Probe too aggressive | `Liveness probe failed` events | Raise `failureThreshold`, check the startup probe budget (60 s) |

Prevention: startup probes give 60 s; the migration Job uses `backoffLimit: 4`.

### 2. `ImagePullBackOff` / `ErrImagePull`

```powershell
kubectl -n ledger describe pod <pod> | Select-String -Pattern 'Failed|pull|manifest|denied'
docker manifest inspect ghcr.io/geve-ops/ledgerflow:<tag>       # does the tag exist?
```

| Cause | Fix |
|---|---|
| The tag in `values-gitops.yaml` has no image yet (the CI release job commits the tag after the push) | Wait for the CI run to finish, or check Actions; re-sync |
| GHCR package is private (anonymous pull fails) | Make the package public, or add an `imagePullSecret` |
| Local image not loaded into Kind (`pullPolicy: Never`, only the `values-local.yaml` path) | `kind load docker-image ledgerflow:dev --name ledgerflow` |
| Registry rate limit / no network | Retry; check Docker Desktop network |

Prevention: pinned tags (`sha-<commit>`); CI publishes before it commits the tag.

### 3. PVC / volume deadlock (node-pinned local volumes)

**Seen here:** draining a node left a Postgres replica `Pending`, and the disruption budget
refused to evict the second replica (`Cannot evict pod ... would violate the pod's disruption
budget`).

```powershell
kubectl -n ledger get pods,pvc
kubectl -n ledger describe pod <pending-pod>        # look for "volume node affinity conflict"
kubectl get pv <pv> -o jsonpath='{.spec.nodeAffinity}'
```

| Situation | Meaning | Fix |
|---|---|---|
| Pod `Pending`, "volume node affinity conflict" | Its volume exists only on a cordoned/down node | `kubectl uncordon <node>` or bring the node back; the pod schedules there |
| `kubectl drain` hangs on a Postgres pod | PDB protecting the last replica/primary (working as designed) | Uncordon, or drain nodes one at a time and wait for `3/3` between them |
| A replica's volume is gone or corrupt | Node lost the directory | Delete the replica pod **and** its PVC (`kubectl -n ledger delete pvc ledger-db-N` then the pod); CloudNativePG re-clones it. **Never delete the primary's PVC** |
| New PVC stuck `Pending` | `WaitForFirstConsumer`: no pod scheduled yet | Normal; check why the pod is unschedulable |

Prevention: network-attached storage removes node pinning; the local-path class has reclaim
`Delete`, so removing a PVC deletes the data.

### 4. Node pressure and evictions

**Seen here:** the cluster shares one machine with Docker Desktop (16 GB assigned), so memory is
the first resource to run out.

```powershell
kubectl describe node <node> | Select-String -Pattern 'Pressure|Allocatable|Allocated'
kubectl top nodes ; kubectl top pods -A --sort-by=memory | Select-Object -First 12
kubectl get events -A --field-selector reason=Evicted
docker stats --no-stream
```

| Symptom | Cause | Fix |
|---|---|---|
| Pods `Evicted`, node `MemoryPressure=True` | Combined requests/usage exceed the Docker VM | Raise Docker Desktop memory; reduce Prometheus retention/limits; lower HPA `maxReplicas` |
| Pods `Pending`, `Insufficient cpu/memory` | Requests exceed free allocatable | Reduce requests or add a node (`cluster/kind-config.yaml`) |
| Everything slow after a long run | Disk or inode pressure on the node containers | `docker system df`; prune images; check the PVC sizes |

Prevention: requests on every container (the Barman sidecar is the exception and should get
some), and the two HPAs capped at 6.

### 5. `OOMKilled`

```powershell
kubectl -n ledger get pod <pod> -o jsonpath='{.status.containerStatuses[*].lastState.terminated.reason}'
kubectl -n ledger describe pod <pod> | Select-String -Pattern 'OOMKilled|Exit Code 137'
kubectl top pod <pod> --containers
```

| Container | Limit | Likely trigger | Fix |
|---|---|---|---|
| API / worker | 256Mi | Large batch plus concurrency, or a leak | Raise the limit; lower `LEDGER_CONCURRENCY` / batch size |
| Redis | 384Mi | Stream grew past `maxmemory 256mb` | **With `noeviction`, Redis refuses writes before it is killed**: the API returns `503` for enqueue. Drain the backlog (add workers), then raise `maxmemory` and the limit together |
| Redis exporter | 64Mi | Many keys | Raise the limit |
| Prometheus | 1500Mi | High cardinality or long retention | Reduce retention/series; raise the limit |
| Postgres | 512Mi | `shared_buffers` + connections + large queries | Raise the limit; reduce `max_connections`/pool sizes |

Prevention: keep `maxmemory` safely below the container limit (it is 256 MB vs 384 Mi), alert on
`RedisMemoryHigh` (80 %), and watch `ledger_stream_lag` so the stream never reaches the cap.

---

## Other issues seen during this project

| Symptom | Cause | Resolution |
|---|---|---|
| HPA shows `<unknown>/40` for the request-rate metric and never scales down | A labelled request counter only appears after a pod's first request, so idle pods had no series | Fixed: a label-free counter (`ledger_api_requests_total`) is exported from process start |
| Argo CD apps `Degraded` right after bootstrap | Ordering between separate Applications is eventual; webhooks not ready yet | Wait; retries resolve it. Not an error unless it persists beyond ~15 minutes |
| `kubectl port-forward` or direct ports 80/443/8080/8443 unreachable from Windows | WSL's relay process intercepts localhost ports Docker publishes | Use `scripts/edge-forward.ps1` (port 9443) |
| Windows `curl` fails with the local CA | Revocation check against a CA with no CRL | `--ssl-no-revoke` |
| First scheduled backup failed ("cluster has no plugin configured") | It fired before pods had rolled to add the sidecar | Re-run after the cluster is healthy: `kubectl apply` a `Backup` |
| CI `release` job failed on `git push` | `main` moved while the image was building | Fixed: rebase and retry in the workflow |

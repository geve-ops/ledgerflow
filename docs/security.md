# Security

What protects the system, how it was verified, and where the protection stops. Related:
[architecture.md](architecture.md) | [operations.md](operations.md) |
[reference/manifests.md](reference/manifests.md#network-policies)

Verified facts below come from commands run against the cluster (shown where useful), not from
reading the manifests alone.

---

## 1. Network isolation

### 1.1 Model

The `ledger` namespace is **default-deny in both directions**. Every allowed flow is a separate,
named policy with explicit pod selectors and ports. Enforcement is by Cilium (eBPF); plain
`networking.k8s.io/v1` NetworkPolicies are used wherever possible, with one `CiliumNetworkPolicy`
where the standard API cannot express the rule (the API server).

```mermaid
flowchart LR
    internet(["Client"]) -->|"HTTPS"| envoy["Envoy proxy<br/>ns envoy-gateway-system"]
    envoy -->|"8000"| api["api"]
    prom["Prometheus<br/>ns monitoring"] -.->|"8000"| api
    prom -.->|"9100"| worker["worker"]
    prom -.->|"9121"| redis["redis"]
    prom -.->|"9187"| pg["postgres"]
    api -->|"6379"| redis
    worker -->|"6379"| redis
    api -->|"5432"| pg
    worker -->|"5432"| pg
    migrate["migrate job"] -->|"5432"| pg
    pg <-->|"5432, 8000<br/>replication, status"| pg
    op["CNPG operator<br/>ns cnpg-system"] -->|"5432, 8000"| pg
    pg -->|"8333"| s3["SeaweedFS<br/>ns backup-storage"]
    pg -.->|"CiliumNetworkPolicy:<br/>kube-apiserver entity"| apiserver["Kubernetes API"]
    allpods["every pod in ledger"] -.->|"UDP/TCP 53"| dns["kube-dns"]
```

Anything not drawn is denied: for example `api` cannot reach `worker`, `worker` cannot reach the
internet, `redis` has no egress at all, and nothing reaches Postgres except the listed callers.

### 1.2 Policy inventory (`gitops/network-policies/ledger.yaml`)

| Policy | Selects | Ingress allowed | Egress allowed |
|---|---|---|---|
| `default-deny-all` | all pods | none | none |
| `allow-dns-egress` | all pods | n/a | `kube-dns` pods in `kube-system`, UDP+TCP 53 |
| `api` | `ledgerflow-api` | Envoy proxy pods (ns `envoy-gateway-system`, `component=proxy`) on **8000**; Prometheus on **8000** | `redis` **6379**; Postgres (`cnpg.io/cluster=ledger-db`) **5432** |
| `worker` | `ledgerflow-worker` | Prometheus on **9100** | `redis` **6379**; Postgres **5432** |
| `migrate` | `ledgerflow-migrate` | none | Postgres **5432** |
| `redis` | `redis` | `api` and `worker` on **6379**; Prometheus on **9121** | none |
| `postgres` | Postgres pods | `api`, `worker`, `migrate` on **5432**; Postgres peers on **5432, 8000**; Prometheus on **9187**; CNPG operator on **5432, 8000** | Postgres peers **5432, 8000**; SeaweedFS (ns `backup-storage`) **8333** |
| `postgres-kube-apiserver` (Cilium) | Postgres pods | n/a | entity `kube-apiserver` (the instance manager talks to the API server) |

Kubelet health probes originate from the node itself, which Cilium always permits, so probes need
no rule.

### 1.3 Verification

A "rogue" pod was started in the `ledger` namespace with no allow-policy and asked to connect to
Redis, the Postgres service and the API:

| | Redis 6379 | Postgres 5432 | API 80 |
|---|---|---|---|
| Before the policies | reachable | reachable | reachable |
| After the policies | **timeout** | **timeout** | **timeout** |

Legitimate traffic was re-tested afterwards (a payment submitted through the TLS edge was posted
and read back), and again after the Prometheus and backup rules were added.

### 1.4 Limits of the isolation

- **Only `ledger` is default-deny.** `monitoring`, `backup-storage`, `envoy-gateway-system`,
  `cnpg-system`, `cert-manager`, `sealed-secrets` and `kube-system` have no default-deny policy
  (the Argo CD chart ships its own policies for `argocd`). A compromised pod there is not
  contained by the network layer.
- **No encryption in transit between pods** except where the application or operator provides it
  (Postgres connections do use TLS 1.3, unverified; Redis and the object store do not). Cilium
  WireGuard encryption and mutual authentication are **off** (`enable-encryption`,
  `mesh-auth-enabled`).
- **No egress filtering by DNS name or to the internet** for the application pods beyond
  "denied by default". Cilium FQDN policies would be the next layer if outbound calls are added.
- The Gateway terminates TLS; the hop Envoy -> API is HTTP inside the cluster.

---

## 2. Pod hardening and access control

### 2.1 Pod Security Standards

| Namespace | Enforce | Warn |
|---|---|---|
| `ledger` | **restricted** | restricted |
| `monitoring`, `backup-storage` | baseline | restricted |
| all others | none (privileged by default) | none |

Application containers satisfy `restricted`:

```yaml
securityContext:                     # pod
  runAsNonRoot: true
  runAsUser: 10001
  runAsGroup: 10001
  fsGroup: 10001
  seccompProfile: {type: RuntimeDefault}
containers:
  - securityContext:                 # container
      allowPrivilegeEscalation: false
      readOnlyRootFilesystem: true
      capabilities: {drop: ["ALL"]}
```

Writable paths are explicit `emptyDir` volumes (`/tmp`, 16 Mi). The image runs as UID 10001
without a login shell. Admission rejects any pod in `ledger` that is privileged, runs as root, or
lacks a seccomp profile. The CNPG and Redis pods were admitted under the same policy.

### 2.2 RBAC

**Application identities.** One ServiceAccount per workload; none is bound to any Role or
ClusterRole, and none mounts a token (`automountServiceAccountToken: false`). Verified:

```text
kubectl auth can-i get secrets -n ledger --as=system:serviceaccount:ledger:ledgerflow-api    -> no
kubectl auth can-i list pods   -n ledger --as=system:serviceaccount:ledger:ledgerflow-worker -> no
kubectl auth can-i create pods -n ledger --as=system:serviceaccount:ledger:redis             -> no
```

The same holds for `ledgerflow-migrate`. The applications never call the Kubernetes API.

| ServiceAccount | Namespace | Roles bound | Token mounted |
|---|---|---|---|
| `ledgerflow-api` | ledger | none | no |
| `ledgerflow-worker` | ledger | none | no |
| `ledgerflow-migrate` | ledger | none | no |
| `redis` | ledger | none | no |
| `ledger-db` | ledger | `Role/ledger-db` (created by CloudNativePG) | yes (the instance manager needs the API) |
| `seaweedfs` | backup-storage | none | no |

**Platform identities.** Operators and controllers get the roles their Helm charts define (not
hand-written here): Argo CD application controller and server, cert-manager (8 scoped
ClusterRoles), CloudNativePG operator, the Barman Cloud plugin
(`Role/ledger-db-barman-cloud` per cluster plus a cluster role), Envoy Gateway, Cilium, Sealed
Secrets, Prometheus Operator, kube-state-metrics and prometheus-adapter. These are broad by
nature (operators manage CRDs and Secrets) and are the largest privilege concentration in the
cluster.

**Argo CD project** (`gitops/bootstrap/project.yaml`): restricts *sources* to this repository and
the pinned chart repositories, but allows any namespace and **any cluster-scoped resource**
(`clusterResourceWhitelist: "*"`). That is convenient for bootstrapping a platform and too broad
for a multi-team setup; split into a platform project and an application project that may only
write to `ledger`.

**Human access.** The Kind admin kubeconfig is the only Kubernetes credential. Argo CD uses its
generated `admin` account (`argocd-initial-admin-secret`); no SSO or per-user roles are defined,
and Grafana has a single admin user. These are acceptable for a local project and would be
replaced by SSO + group-mapped roles in a shared environment.

---

## 3. Secrets

### 3.1 Inventory and flow

| Secret | Contents | Created by | Consumed by |
|---|---|---|---|
| `ledger-secrets` | `redis-password`, `api-keys` (`key:client`) | SealedSecret (`gitops/data`) | Redis, API, worker (env) |
| `ledger-db-app` | `username`, `password`, `uri` | CloudNativePG (generated) | API, worker, migrate (env) |
| `ledger-db-ca/-server/-replication` | TLS material | CloudNativePG | Postgres pods |
| `s3-credentials` (ledger, backup-storage) | access key id/secret | SealedSecret (`gitops/backup`) | Barman sidecar, bucket Job |
| `seaweedfs-s3` | S3 identity config | SealedSecret | SeaweedFS (mounted file) |
| `grafana-admin` | admin user/password | SealedSecret (`gitops/observability`) | Grafana |
| `ledgerflow-tls`, `ledgerflow-root-ca` | gateway certificate / local CA | cert-manager | Envoy, clients |

```mermaid
flowchart LR
    dev["Operator"] -->|"kubeseal encrypts with the<br/>controller's public key"| git[("Git: SealedSecret<br/>ciphertext only")]
    git -->|"Argo CD applies"| ss["SealedSecret in cluster"]
    key[("Controller private key<br/>Secret in ns sealed-secrets")] --> ctl["Sealed Secrets controller"]
    ss --> ctl
    ctl -->|"decrypts"| sec["Kubernetes Secret"]
    sec -->|"secretKeyRef env var"| pod["Pod"]
```

Plaintext never enters Git: the repository contains only ciphertext, and a check of the
generated manifest reference found none of the real secret values.

### 3.2 Injection

Secrets reach pods as environment variables through `secretKeyRef`. The database URI for read
replicas is assembled in the pod spec from separate variables:

```yaml
- name: DB_USER      # secretKeyRef ledger-db-app/username
- name: DB_PASSWORD  # secretKeyRef ledger-db-app/password
- name: LEDGER_DATABASE_RO_URL
  value: "postgresql://$(DB_USER):$(DB_PASSWORD)@ledger-db-ro...:5432/ledger"
```

Trade-off: environment variables are visible to anything that can `exec` into the pod or read
`/proc/<pid>/environ`, and appear in a crash dump. Mounting secrets as files (as done for the
SeaweedFS config) avoids that and allows rotation without a restart. Moving the API and worker to
file-based secrets is the natural hardening step.

### 3.3 Key custody and rotation (important)

- The Sealed Secrets **private key is the single point of failure**: without it, a rebuilt
  cluster cannot decrypt anything in Git. `scripts/backup-sealing-key.ps1` exports it to
  `.local/sealed-secrets-key.json`, which is git-ignored and must be copied somewhere safe.
- The controller **generates a new sealing key periodically** (default every 30 days) and keeps
  the old ones. Secrets sealed after a rotation need the *new* key to be decryptable after a
  rebuild, so **re-run the backup script after each rotation** (or at least monthly).
- Rotating an application secret: re-seal the new value with `kubeseal`, commit, and let Argo CD
  apply; restart consumers (environment variables are read at start).
- The database credentials in `ledger-db-app` are generated and owned by CloudNativePG. Rotate
  them through its documented procedure rather than editing the Secret by hand, then restart the
  consumers.

---

## 4. Image and supply chain

| Control | Where |
|---|---|
| Image built in CI from a pinned base (`python:3.12-slim`), non-root user, no shell login | `services/app/Dockerfile` |
| Vulnerability scan (Trivy, fails on **fixable CRITICAL**) before the image is pushed | `.github/workflows/ci.yaml` |
| Manifests validated against Kubernetes schemas (`kubeconform -strict`) and `helm lint` | CI |
| Tests run against real Postgres and Redis containers before any image is published | CI |
| Third-party charts and images pinned to explicit versions (no `latest`) | Argo CD apps, manifests |
| Images are **not signed** and there is no admission policy requiring signatures | Gap (cosign + a policy engine would close it) |
| No SBOM is generated or stored | Gap |

---

## 5. Threat summary

| Threat | Mitigation present | Residual |
|---|---|---|
| Replay or double-submit of a payment | Required idempotency key; body fingerprint; DB unique constraint | Key space is per client; a stolen API key can still submit new payments |
| Brute-force or abuse by one client | Per-client fixed-window rate limit | Fixed window allows bursts at window edges |
| Compromised app pod moving laterally | Default-deny in `ledger`; no API token; non-root, read-only filesystem, no capabilities | Other namespaces are not default-deny |
| Ledger tampering | Append-only triggers on ledger and audit tables; balanced-entries invariant endpoint | A database superuser can still drop triggers; audit shipping off-cluster is not implemented |
| Secret exposure from Git | SealedSecrets only | Key custody (section 3.3); env-var exposure inside pods |
| Plaintext on the wire inside the cluster | Postgres TLS 1.3 (unverified) | Redis, object store and Envoy -> API are plaintext; no mesh |
| Supply-chain compromise | Pinned versions, CI scan | No signing, no SBOM, no admission enforcement |

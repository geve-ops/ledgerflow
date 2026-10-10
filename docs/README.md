# Documentation

| Document | Contents |
|---|---|
| [architecture.md](architecture.md) | System overview, every component and resource, storage, four Mermaid diagrams (end-to-end, request lifecycle, persistence, failure recovery), honest gaps |
| [security.md](security.md) | NetworkPolicies and their verification, Pod Security, RBAC, secrets and key custody, supply chain, threat summary |
| [observability.md](observability.md) | Logging, metrics inventory, dashboard, alert rules, SLIs/SLOs, exact probe settings |
| [operations.md](operations.md) | Sizing, HPAs, VPA proposal, scaling, disaster recovery and migration runbooks, top-5 troubleshooting |
| [backups.md](backups.md) | Backup design and the verified point-in-time restore |
| [chaos.md](chaos.md) | Failure experiments with measured results |
| [reference/manifests.md](reference/manifests.md) | Every manifest, generated from the repository (do not edit by hand) |

Conventions: **measured** means a result from a test in this repository; **proposed** means not
deployed; **not tested** means the mechanism is configured but the scenario was not exercised.
Regenerate the manifest reference with `python scripts/gen_manifest_reference.py` after changing
any manifest.

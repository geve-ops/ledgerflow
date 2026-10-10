# Backups and point-in-time recovery

The ledger database is backed up with the CloudNativePG **Barman Cloud plugin**: a daily base
backup plus **continuous WAL archiving** to an object store, which allows restoring to any
moment, not just to the time of a backup.

```
Postgres pods (with a backup sidecar)
   |  base backup (daily, 02:00)         \
   |  WAL segments (continuously)         >  s3://cnpg-backups/   (SeaweedFS, S3-compatible)
   v                                     /
ObjectStore "ledger-store"  (retention: 7 days)
```

| Piece | Where |
|---|---|
| Plugin | `gitops/platform/barman-plugin` (installed by Argo CD before the database) |
| Object store | `gitops/backup/seaweedfs.yaml`; credentials are Sealed Secrets |
| What to back up, and where | `gitops/backup/objectstore.yaml` |
| Cluster archiving + daily schedule | `gitops/data/cnpg-cluster.yaml`, `gitops/data/backup-schedule.yaml` |
| Network access | the Postgres NetworkPolicy allows egress to the store on port 8333 only |

## Restore test

`scripts/restore-test.ps1` builds a throwaway cluster from the object store and I compared it
with the live database.

1. Took a base backup (finished in about 21 s with ~188,000 transactions).
2. Recorded the exact state and a timestamp **T1**: 187,904 transactions, 24 accounts, a
   checksum of all ledger amounts, 187,928 audit rows.
3. Simulated a mistake after T1: inserted two bogus accounts and an audit row, and forced the
   WAL segment to archive.
4. Restored a new cluster with `recoveryTarget.targetTime = T1`.

| | Restored cluster | Live cluster |
|---|---|---|
| transactions \| accounts \| sum of amounts \| audit rows | **187904 \| 24 \| 28094292770 \| 187928** (identical to T1) | includes the mistake |
| Bogus accounts / audit row | **0 / 0** | 2 / 1 |
| Debits = credits, balances sum to 0 | yes | yes |

```powershell
./scripts/restore-test.ps1 -TargetTime '2026-10-10 09:10:29.550108+00'
kubectl -n restore-test get cluster ledger-restored
./scripts/restore-test.ps1 -Cleanup
```

## Caveats

- The object store is **one pod with a local volume** in the same cluster. That proves the
  mechanism, but it is not a durable backup target: a real deployment writes to a bucket in a
  different failure domain (another region or account).
- SeaweedFS stands in for S3 because MinIO's community images are no longer published. Only the
  `ObjectStore` endpoint and credentials change when switching to real S3.
- The audit log is append-only by design, so the audit row from the simulated mistake remains in
  the live database. It is a harmless, clearly labelled entry (`oops.mistake`).
- WAL archiving failed 13 times while the pods were rolling to add the backup sidecar, before the
  sidecar was ready. It has been healthy since (`pg_stat_archiver`).
- When the plugin was added to an already-running cluster, the first scheduled backup failed with "cluster has no plugin configured" because it fired
  (`immediate: true`) before the pods had been rolled. The on-demand backup taken afterwards
  succeeded; the daily 02:00 schedule has not yet run at the time of writing.
In a from-scratch rebuild, where the plugin is part of the cluster from the start, the first scheduled backup completed on its own.

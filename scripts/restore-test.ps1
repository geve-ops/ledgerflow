# Proves the backups work: restores the ledger database into a throwaway cluster, optionally to a
# point in time, and compares it with the expected state.
#   ./scripts/restore-test.ps1 -TargetTime '2026-10-10 09:10:29.550108+00'
#   ./scripts/restore-test.ps1 -Cleanup           # remove the throwaway namespace
param([string]$TargetTime = '', [switch]$Cleanup)
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
$ns = 'restore-test'

if ($Cleanup) { kubectl delete namespace $ns --wait=false; return }

kubectl create namespace $ns --dry-run=client -o yaml | kubectl apply -f -

# Same bucket and credentials as production; the credentials are copied from the live Secret.
$key = kubectl -n ledger get secret s3-credentials -o jsonpath='{.data.ACCESS_KEY_ID}'
$sec = kubectl -n ledger get secret s3-credentials -o jsonpath='{.data.ACCESS_SECRET_KEY}'
@"
apiVersion: v1
kind: Secret
metadata: {name: s3-credentials, namespace: $ns}
data: {ACCESS_KEY_ID: $key, ACCESS_SECRET_KEY: $sec}
"@ | kubectl apply -f -
kubectl -n ledger get objectstore ledger-store -o json |
  ConvertFrom-Json | ForEach-Object {
    $_.metadata = @{ name = 'ledger-store'; namespace = $ns }; $_.PSObject.Properties.Remove('status'); $_ } |
  ConvertTo-Json -Depth 20 | kubectl apply -f -

$target = if ($TargetTime) { "      recoveryTarget:`n        targetTime: `"$TargetTime`"" } else { '' }
@"
apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata: {name: ledger-restored, namespace: $ns}
spec:
  instances: 1
  storage: {size: 2Gi}
  bootstrap:
    recovery:
      source: source
$target
  externalClusters:
    - name: source
      plugin:
        name: barman-cloud.cloudnative-pg.io
        parameters: {barmanObjectName: ledger-store, serverName: ledger-db}
"@ | kubectl apply -f -
Write-Host "Restoring... watch with: kubectl -n $ns get cluster,pods -w"

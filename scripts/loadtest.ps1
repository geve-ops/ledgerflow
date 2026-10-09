# Runs the k6 load test inside the cluster and prints the result.
#   ./scripts/loadtest.ps1 -PeakRps 300
param([int]$PeakRps = 300)
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)

$key = ((Get-Content .local/secrets.env) | Where-Object { $_ -like 'API_KEY=*' }) -replace 'API_KEY=', ''
$gw = kubectl -n envoy-gateway-system get svc -l gateway.envoyproxy.io/owning-gateway-name=ledgerflow `
  -o jsonpath='{.items[0].spec.clusterIP}'

kubectl create namespace loadtest --dry-run=client -o yaml | kubectl apply -f -
kubectl -n loadtest delete job k6 --ignore-not-found | Out-Null
kubectl -n loadtest create configmap k6-script --from-file=ledger.js=load-tests/ledger.js `
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n loadtest create secret generic k6-api-key --from-literal=api-key=$key `
  --dry-run=client -o yaml | kubectl apply -f -
(Get-Content load-tests/job.yaml -Raw).Replace('__GATEWAY_IP__', $gw).Replace('__PEAK_RPS__', "$PeakRps") |
  kubectl apply -f -

Write-Host "k6 running (~6.5 min). Follow with: kubectl -n loadtest logs -f job/k6"

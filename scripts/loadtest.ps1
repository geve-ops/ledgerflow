# Runs the k6 load test inside the cluster and prints the result.
#   ./scripts/loadtest.ps1 -PeakRps 300
#   ./scripts/loadtest.ps1 -SteadyRps 40 -Duration 3m   # constant rate, for failure injection
#   -PinNode <name> keeps the load generator on one node (so draining another node cannot evict it)
param([int]$PeakRps = 300, [int]$SteadyRps = 0, [string]$Duration = '3m', [string]$PinNode = '')
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
$node = if ($PinNode) { "nodeSelector: {kubernetes.io/hostname: $PinNode}" } else { '' }
$yaml = Get-Content load-tests/job.yaml -Raw
$yaml = $yaml.Replace('__GATEWAY_IP__', $gw).Replace('__PEAK_RPS__', "$PeakRps")
$yaml = $yaml.Replace('__STEADY_RPS__', "$SteadyRps").Replace('__DURATION__', $Duration)
$yaml = $yaml.Replace('# __NODE__', $node)
$yaml | kubectl apply -f -
if ($LASTEXITCODE -ne 0) { throw 'failed to create the k6 job' }

Write-Host "k6 running. Follow with: kubectl -n loadtest logs -f job/k6"

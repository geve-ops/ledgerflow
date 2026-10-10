# Chaos test: drain a worker node while the API takes steady traffic, then restore it.
#   ./scripts/chaos-drain.ps1 [-Node ledgerflow-worker2]
# Writes a timeline to .local/chaos-drain.txt.
param([string]$Node = 'ledgerflow-worker2')
$ErrorActionPreference = 'Continue'
Set-Location (Split-Path $PSScriptRoot -Parent)
$log = '.local/chaos-drain.txt'
function Note($m) { $line = "{0:HH:mm:ss}  {1}" -f (Get-Date), $m; $line | Tee-Object -FilePath $log -Append }
function Snapshot {
  $pods = kubectl -n ledger get pods -o json | ConvertFrom-Json
  $by = $pods.items | Group-Object { ($_.metadata.labels.'app.kubernetes.io/name') } | ForEach-Object {
    $r = ($_.Group | Where-Object { $_.status.phase -eq 'Running' -and ($_.status.containerStatuses | Where-Object { -not $_.ready }).Count -eq 0 }).Count
    "{0}={1}/{2}" -f $_.Name, $r, $_.Count }
  ($by -join '  ')
}

'' | Set-Content $log
$other = (kubectl get nodes -l '!node-role.kubernetes.io/control-plane' -o name | ForEach-Object { $_ -replace 'node/', '' } | Where-Object { $_ -ne $Node } | Select-Object -First 1)
Note "load generator pinned to $other (not drained)"
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/loadtest.ps1 -SteadyRps 40 -Duration 3m -PinNode $other | Out-Null
if ($LASTEXITCODE -ne 0) { Note 'ABORT: load generator failed to start'; return }
Note 'k6 started: 40 req/s for 3m'
Start-Sleep -Seconds 40
Note "before drain: $(Snapshot)"
Note "DRAIN $Node (timeout 100s)"
$out = kubectl drain $Node --ignore-daemonsets --delete-emptydir-data --timeout=100s --force 2>&1
$out | Where-Object { $_ -match 'evict|error|disruption|drained|timed out|There are pending' } | Select-Object -First 14 | ForEach-Object { Note "  drain: $_" }
Note "after drain attempt: $(Snapshot)"
Start-Sleep -Seconds 5
kubectl uncordon $Node | Out-Null
Note "UNCORDONED $Node"
for ($i = 0; $i -lt 40; $i++) {
  if ((kubectl -n ledger get cluster ledger-db -o jsonpath='{.status.phase}') -eq 'Cluster in healthy state') { Note "cluster healthy again: $(Snapshot)"; break }
  Start-Sleep -Seconds 5
}
Start-Sleep -Seconds 45

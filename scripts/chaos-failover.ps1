# Chaos test: kill the Postgres primary while the API is taking steady traffic.
#   ./scripts/chaos-failover.ps1
# Writes a timeline to .local/chaos-failover.txt.
$ErrorActionPreference = 'Continue'
Set-Location (Split-Path $PSScriptRoot -Parent)
$log = '.local/chaos-failover.txt'
function Note($m) { $line = "{0:HH:mm:ss}  {1}" -f (Get-Date), $m; $line | Tee-Object -FilePath $log -Append }
function Lag { (kubectl -n ledger exec redis-0 -c redis -- redis-cli XINFO GROUPS ledger:events 2>$null | Select-String -Pattern '^\d+$' | ForEach-Object { $_.Line } | Select-Object -Last 2) -join '/' }
function Primary { kubectl -n ledger get cluster ledger-db -o jsonpath='{.status.currentPrimary}' }
function Phase { kubectl -n ledger get cluster ledger-db -o jsonpath='{.status.phase}' }

'' | Set-Content $log
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/loadtest.ps1 -SteadyRps 40 -Duration 3m | Out-Null
if ($LASTEXITCODE -ne 0) { Note 'ABORT: load generator failed to start'; return }
Note 'k6 started: 40 req/s for 3m'
Start-Sleep -Seconds 60
$old = Primary
Note "primary before: $old  phase='$(Phase)'"
$t0 = Get-Date
kubectl -n ledger delete pod $old --force --grace-period=0 2>$null | Out-Null
Note "KILLED $old"
$promoted = $null; $healthy = $null
for ($i = 0; $i -lt 60; $i++) {
  $p = Primary; $ph = Phase
  if (-not $promoted -and $p -and $p -ne $old) { $promoted = (Get-Date) - $t0; Note ("new primary $p after {0:n1}s" -f $promoted.TotalSeconds) }
  if ($ph -eq 'Cluster in healthy state' -and $promoted) { $healthy = (Get-Date) - $t0; Note ("cluster healthy again (3 instances) after {0:n1}s" -f $healthy.TotalSeconds); break }
  Start-Sleep -Seconds 3
}
Note "stream lag/pending/etc: $(Lag)"
Start-Sleep -Seconds 100
Note "after load: stream info $(Lag)"

# Chaos test: roll out a config change through Git + Argo CD while the API takes steady traffic.
#   ./scripts/chaos-rollout.ps1
# Writes a timeline to .local/chaos-rollout.txt.
$ErrorActionPreference = 'Continue'
Set-Location (Split-Path $PSScriptRoot -Parent)
$log = '.local/chaos-rollout.txt'
function Note($m) { $line = "{0:HH:mm:ss}  {1}" -f (Get-Date), $m; $line | Tee-Object -FilePath $log -Append }
$gitArgs = @('-c', 'credential.helper=', '-c', 'credential.helper=!gh auth git-credential')
$env:GIT_TERMINAL_PROMPT = '0'

'' | Set-Content $log
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/loadtest.ps1 -SteadyRps 40 -Duration 2m30s | Out-Null
if ($LASTEXITCODE -ne 0) { Note 'ABORT: load generator failed to start'; return }
Note 'k6 started: 40 req/s for 2m30s'
Start-Sleep -Seconds 40

# A real change: bump the rate limit (changes the pod template -> rolling update)
$f = 'charts/ledgerflow/values-gitops.yaml'
$old = (Select-String -Path $f -Pattern 'rateLimitPerMinute: (\d+)').Matches[0].Groups[1].Value
$new = [int]$old + 1000
(Get-Content $f -Raw) -replace "rateLimitPerMinute: $old", "rateLimitPerMinute: $new" | Set-Content $f -Encoding ascii -NoNewline
git add $f; git commit -q -m "Rolling-update test: rate limit $old -> $new [skip ci]"
git @gitArgs push origin main 2>&1 | Out-Null
$t0 = Get-Date
Note "pushed change (rate limit $old -> $new)"
kubectl -n argocd annotate app ledgerflow argocd.argoproj.io/refresh=hard --overwrite | Out-Null

$minReady = 99; $started = $null; $done = $null
for ($i = 0; $i -lt 90; $i++) {
  $d = kubectl -n ledger get deploy ledgerflow-api -o json | ConvertFrom-Json
  $ready = [int]$d.status.readyReplicas; $upd = [int]$d.status.updatedReplicas; $tot = [int]$d.status.replicas
  $val = ($d.spec.template.spec.containers[0].env | Where-Object name -eq 'LEDGER_RATE_LIMIT_PER_MINUTE').value
  if ($ready -lt $minReady) { $minReady = $ready }
  if (-not $started -and $val -eq "$new") { $started = (Get-Date) - $t0; Note ("rollout began after {0:n0}s" -f $started.TotalSeconds) }
  if ($started -and $upd -eq $tot -and $ready -eq $tot -and $d.status.unavailableReplicas -eq $null) {
    $done = (Get-Date) - $t0; Note ("rollout complete after {0:n0}s; replicas={1}" -f $done.TotalSeconds, $tot); break }
  Start-Sleep -Seconds 3
}
Note "lowest ready API replicas during rollout: $minReady"
Start-Sleep -Seconds 75

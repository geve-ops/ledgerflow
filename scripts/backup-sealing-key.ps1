# Exports the Sealed Secrets private key to .local/ (gitignored). Keep it somewhere safe:
# without it a rebuilt cluster cannot decrypt the SealedSecrets stored in Git.
Set-Location (Split-Path $PSScriptRoot -Parent)
New-Item -ItemType Directory -Force .local | Out-Null
$list = kubectl -n sealed-secrets get secret -l sealedsecrets.bitnami.com/sealed-secrets-key -o json | ConvertFrom-Json
foreach ($item in $list.items) {
  foreach ($f in 'uid', 'resourceVersion', 'creationTimestamp', 'managedFields') {
    $item.metadata.PSObject.Properties.Remove($f)
  }
}
$list | ConvertTo-Json -Depth 20 | Set-Content .local/sealed-secrets-key.json -Encoding ascii
Write-Host "Saved $($list.items.Count) key(s) to .local/sealed-secrets-key.json"

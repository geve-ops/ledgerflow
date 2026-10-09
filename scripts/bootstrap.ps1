# Rebuilds the whole platform from nothing: Kind cluster -> Cilium -> Argo CD -> app-of-apps.
# Everything after the root Application is installed by Argo CD from Git.
#
#   ./scripts/bootstrap.ps1            # create and bootstrap
#   ./scripts/bootstrap.ps1 -Destroy   # delete the cluster
param([switch]$Destroy)
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)

if ($Destroy) { kind delete cluster --name ledgerflow; return }

Write-Host '==> Creating Kind cluster (3 nodes, default CNI disabled)'
kind create cluster --config cluster/kind-config.yaml --wait 0s

Write-Host '==> Installing Cilium (CNI; must exist before anything can schedule)'
helm repo add cilium https://helm.cilium.io | Out-Null
helm upgrade --install cilium cilium/cilium --version 1.20.2 -n kube-system `
  -f cluster/cilium-values.yaml --wait --timeout 8m

# The Sealed Secrets key must exist BEFORE the controller starts, otherwise a fresh key is
# generated and the SealedSecrets committed to Git can no longer be decrypted.
if (Test-Path .local/sealed-secrets-key.json) {
  Write-Host '==> Restoring Sealed Secrets key'
  kubectl create namespace sealed-secrets --dry-run=client -o yaml | kubectl apply -f -
  kubectl apply -f .local/sealed-secrets-key.json
} else {
  Write-Warning 'No .local/sealed-secrets-key.json: existing SealedSecrets will NOT decrypt. Re-seal them.'
}

Write-Host '==> Installing Argo CD'
helm repo add argo https://argoproj.github.io/argo-helm | Out-Null
helm upgrade --install argocd argo/argo-cd --version 10.10.1 -n argocd --create-namespace `
  -f gitops/platform/argocd-values.yaml --wait --timeout 8m

Write-Host '==> Handing over to GitOps (root app-of-apps)'
kubectl apply -f gitops/bootstrap/project.yaml
kubectl apply -f gitops/bootstrap/root-app.yaml
Write-Host 'Done. Watch progress with: kubectl -n argocd get applications -w'

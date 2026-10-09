# Exposes the Envoy Gateway on https://ledgerflow.localtest.me:9443 (and http on :9080).
# localtest.me resolves to 127.0.0.1, so no hosts-file edit is needed.
#
# Export the local CA once so clients can verify the certificate:
#   kubectl -n cert-manager get secret ledgerflow-root-ca -o jsonpath='{.data.ca\.crt}'
$svc = kubectl -n envoy-gateway-system get svc `
  -l gateway.envoyproxy.io/owning-gateway-name=ledgerflow -o jsonpath='{.items[0].metadata.name}'
Write-Host "Forwarding svc/$svc -> https://ledgerflow.localtest.me:9443 (Ctrl+C to stop)"
kubectl -n envoy-gateway-system port-forward "svc/$svc" 9443:443 9080:80

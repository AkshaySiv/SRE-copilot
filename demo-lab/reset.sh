#!/usr/bin/env bash
# Put the lab back to its healthy baseline.
set -euo pipefail
CTX="kind-sre-lab"
cd "$(dirname "$0")"
kubectl --context "$CTX" delete deploy -n platform batch-importer --ignore-not-found
kubectl --context "$CTX" delete -f apps.yaml --ignore-not-found --wait=false >/dev/null 2>&1 || true
kubectl --context "$CTX" wait --for=delete ns/shop ns/payments ns/platform --timeout=120s 2>/dev/null || true
kubectl --context "$CTX" apply -f apps.yaml
echo "Lab reset to healthy baseline."

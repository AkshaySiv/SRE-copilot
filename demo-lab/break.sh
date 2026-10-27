#!/usr/bin/env bash
# Inject realistic incidents into the lab. Each one can be enabled on its own:
#   ./break.sh            -> all incidents
#   ./break.sh payments   -> only the bad payments release
set -eo pipefail   # (no -u: macOS ships bash 3.2)
CTX="kind-sre-lab"
k() { kubectl --context "$CTX" "$@"; }
ALL=0; [[ $# -eq 0 ]] && ALL=1
LIST=" $* "
want() { [[ $ALL == 1 || "$LIST" == *" $1 "* ]]; }

if want payments; then
  echo "[payments]  Bad release 2.4.0: DB_POOL_SIZE=twenty  -> CrashLoopBackOff, checkout 503s (fix: rollback)"
  k -n payments annotate deploy/payments-api kubernetes.io/change-cause="helm upgrade payments 2.4.0 (pipeline #4512)" --overwrite
  k -n payments set env deploy/payments-api APP_VERSION=2.4.0 DB_POOL_SIZE=twenty FEATURE_FAST_REFUNDS=true
fi
if want oom; then
  echo "[payments]  ledger-worker cache grows to 300MB with a 96Mi limit -> OOMKilled"
  k -n payments set env deploy/ledger-worker CACHE_MB=300
fi
if want selector; then
  echo "[shop]      checkout Service selector typo -> no endpoints"
  k -n shop patch svc checkout --type merge -p '{"spec":{"selector":{"app":"checkout-v2"}}}'
fi
if want image; then
  echo "[platform]  notifications image tag that does not exist -> ImagePullBackOff"
  k -n platform set image deploy/notifications notifications=nginx:1.99.99-doesnotexist
fi
if want config; then
  echo "[platform]  report-generator references a missing ConfigMap -> CreateContainerConfigError"
  k -n platform patch deploy report-generator --type json -p \
    '[{"op":"add","path":"/spec/template/spec/containers/0/envFrom","value":[{"configMapRef":{"name":"report-settings"}}]}]'
fi
if want pending; then
  echo "[platform]  batch-importer requests 64 CPUs -> Pending / unschedulable"
  k -n platform create deployment batch-importer --image=busybox:1.36 --dry-run=client -o yaml -- sleep 3600 \
    | sed 's/resources: {}/resources: { requests: { cpu: "64" } }/' | k apply -f -
fi
echo
echo "Give it ~1 minute for crash loops and back-offs to show up, then ask the copilot what is wrong."

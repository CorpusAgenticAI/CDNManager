#!/usr/bin/env bash
# Stop or start the hosted CDN Manager to save cost. Data in Cloud SQL is kept while stopped.
# Run from the project root in Cloud Shell:
#   bash deploy/gcp/control.sh status
#   bash deploy/gcp/control.sh stop
#   bash deploy/gcp/control.sh start
set -euo pipefail
cd "$(dirname "$0")/../.."
source deploy/gcp/config.sh

say() { printf '\n==> %s\n' "$*"; }
service_url() { gcloud run services describe "${SERVICE}" --region="${REGION}" --format='value(status.url)'; }
http_code() { curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$1" || true; }

status() {
  local sql url
  sql="$(gcloud sql instances describe "${SQL_INSTANCE}" --format='value(state,settings.activationPolicy)')"
  url="$(service_url)"
  printf 'Database  %s  (%s)\n' "${SQL_INSTANCE}" "${sql}"
  printf 'App       %s\n' "${url}"
  printf 'Readiness HTTP %s  (200 = running; 404 or 403 = stopped)\n' "$(http_code "${url}/readyz")"
}

stop() {
  say "Closing the app to traffic and scaling it to zero"
  gcloud run services update "${SERVICE}" --region="${REGION}" --ingress=internal --min-instances=0 --quiet
  say "Stopping Cloud SQL (storage and backups are kept)"
  gcloud sql instances patch "${SQL_INSTANCE}" --activation-policy=NEVER --quiet
  say "Stopped. Start again with: bash deploy/gcp/control.sh start"
}

start() {
  say "Starting Cloud SQL (about 1 to 2 minutes)"
  gcloud sql instances patch "${SQL_INSTANCE}" --activation-policy=ALWAYS --quiet
  say "Opening the app and keeping ${MIN_INSTANCES} instance(s) warm"
  gcloud run services update "${SERVICE}" --region="${REGION}" --ingress=all --min-instances="${MIN_INSTANCES}" --quiet
  local url code
  url="$(service_url)"
  say "Waiting for ${url}/readyz"
  for _ in $(seq 1 30); do
    code="$(http_code "${url}/readyz")"
    if [[ "${code}" == "200" ]]; then
      say "Running: ${url}"
      return 0
    fi
    sleep 5
  done
  echo "Not ready after 150 s (last HTTP ${code}). Check: bash deploy/gcp/control.sh status" >&2
  return 1
}

case "${1:-status}" in
  start) start ;;
  stop) stop ;;
  status) status ;;
  *) echo "Usage: bash deploy/gcp/control.sh start|stop|status" >&2; exit 2 ;;
esac

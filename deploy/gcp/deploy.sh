#!/usr/bin/env bash
# Build the image in Cloud Build, run database migrations, and deploy to Cloud Run.
# Run from the project root in Cloud Shell after setup.sh:  bash deploy/gcp/deploy.sh
set -euo pipefail
cd "$(dirname "$0")/../.."
source deploy/gcp/config.sh

gcloud builds submit \
  --project="${PROJECT_ID}" \
  --config=cloudbuild.yaml \
  --substitutions="_REGION=${REGION},_SERVICE=${SERVICE},_REPO=${REPO},_SQL_INSTANCE=${SQL_INSTANCE},_DB_NAME=${DB_NAME},_DB_USER=${DB_USER},_ADMIN_USERNAME=${ADMIN_USERNAME},_RUN_SA_NAME=${RUN_SA_NAME},_MIN_INSTANCES=${MIN_INSTANCES},_MAX_INSTANCES=${MAX_INSTANCES},_CPU=${CPU},_MEMORY=${MEMORY}" \
  .

url="$(gcloud run services describe "${SERVICE}" --region="${REGION}" --format='value(status.url)')"
printf '\nService URL: %s\n' "${url}"
printf 'Readiness:   '
curl -fsS "${url}/readyz" && echo
printf '\nFirst sign-in: user "%s", password from:\n  gcloud secrets versions access latest --secret=%s\n' "${ADMIN_USERNAME}" "${ADMIN_PASSWORD_SECRET}"

#!/usr/bin/env bash
# One-time Google Cloud setup for CDN Manager. Safe to re-run: existing resources are left alone.
# Run from the project root in Cloud Shell:  bash deploy/gcp/setup.sh
set -euo pipefail
cd "$(dirname "$0")/../.."
source deploy/gcp/config.sh

say() { printf '\n==> %s\n' "$*"; }
random_password() { openssl rand -base64 48 | tr -d '/+=\n' | cut -c1-32; }

say "Project ${PROJECT_ID}, region ${REGION}"
gcloud config set project "${PROJECT_ID}" >/dev/null

say "Enabling APIs"
gcloud services enable \
  run.googleapis.com \
  sqladmin.googleapis.com \
  secretmanager.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  iam.googleapis.com

say "Artifact Registry repository ${REPO}"
if ! gcloud artifacts repositories describe "${REPO}" --location="${REGION}" >/dev/null 2>&1; then
  gcloud artifacts repositories create "${REPO}" \
    --repository-format=docker \
    --location="${REGION}" \
    --description="CDN Manager images"
fi

say "Cloud SQL instance ${SQL_INSTANCE} (first run takes about 10 minutes)"
if ! gcloud sql instances describe "${SQL_INSTANCE}" >/dev/null 2>&1; then
  gcloud sql instances create "${SQL_INSTANCE}" \
    --database-version=POSTGRES_16 \
    --edition=ENTERPRISE \
    --tier="${SQL_TIER}" \
    --region="${REGION}" \
    --availability-type="${SQL_AVAILABILITY}" \
    --storage-type=SSD \
    --storage-size=20GB \
    --storage-auto-increase \
    --backup-start-time=20:30 \
    --enable-point-in-time-recovery \
    --retained-backups-count=14 \
    --maintenance-window-day=SUN \
    --maintenance-window-hour=21 \
    --deletion-protection
fi

say "Database ${DB_NAME}"
if ! gcloud sql databases describe "${DB_NAME}" --instance="${SQL_INSTANCE}" >/dev/null 2>&1; then
  gcloud sql databases create "${DB_NAME}" --instance="${SQL_INSTANCE}"
fi

say "Secrets"
for secret in "${DB_PASSWORD_SECRET}" "${ADMIN_PASSWORD_SECRET}"; do
  if ! gcloud secrets describe "${secret}" >/dev/null 2>&1; then
    random_password | gcloud secrets create "${secret}" --replication-policy=automatic --data-file=-
  fi
done

say "Database user ${DB_USER}"
db_password="$(gcloud secrets versions access latest --secret="${DB_PASSWORD_SECRET}")"
if gcloud sql users list --instance="${SQL_INSTANCE}" --format='value(name)' | grep -qx "${DB_USER}"; then
  gcloud sql users set-password "${DB_USER}" --instance="${SQL_INSTANCE}" --password="${db_password}"
else
  gcloud sql users create "${DB_USER}" --instance="${SQL_INSTANCE}" --password="${db_password}"
fi
unset db_password

say "Runtime service account ${RUN_SA}"
if ! gcloud iam service-accounts describe "${RUN_SA}" >/dev/null 2>&1; then
  gcloud iam service-accounts create "${RUN_SA_NAME}" --display-name="CDN Manager (Cloud Run)"
fi
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${RUN_SA}" --role=roles/cloudsql.client --condition=None >/dev/null
for secret in "${DB_PASSWORD_SECRET}" "${ADMIN_PASSWORD_SECRET}"; do
  gcloud secrets add-iam-policy-binding "${secret}" \
    --member="serviceAccount:${RUN_SA}" --role=roles/secretmanager.secretAccessor >/dev/null
done

say "Cloud Build service account permissions"
build_sa="$(gcloud builds get-default-service-account --format='value(serviceAccountEmail)' | sed 's#.*/##')"
for role in roles/cloudbuild.builds.builder roles/run.admin; do
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${build_sa}" --role="${role}" --condition=None >/dev/null
done
gcloud iam service-accounts add-iam-policy-binding "${RUN_SA}" \
  --member="serviceAccount:${build_sa}" --role=roles/iam.serviceAccountUser >/dev/null

say "Setup complete. Next: bash deploy/gcp/deploy.sh"

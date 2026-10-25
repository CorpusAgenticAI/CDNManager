#!/usr/bin/env bash
# Let GitHub Actions on the main branch of one repository deploy CDN Manager, without a stored key.
# Run in Cloud Shell after setup.sh:  GITHUB_REPO=owner/name bash deploy/gcp/github-setup.sh
# GITHUB_REPO must match GitHub's spelling exactly, including upper and lower case.
set -euo pipefail
cd "$(dirname "$0")/../.."
source deploy/gcp/config.sh
: "${GITHUB_REPO:?Set GITHUB_REPO=owner/name (exact case)}"

POOL="${POOL:-github}"
PROVIDER="${PROVIDER:-github-oidc}"
DEPLOY_SA_NAME="${DEPLOY_SA_NAME:-cdnmanager-deploy}"
DEPLOY_SA="${DEPLOY_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
BUCKET="${PROJECT_ID}-cdnmanager-build"
CONDITION="assertion.repository=='${GITHUB_REPO}' && assertion.ref=='refs/heads/main'"

say() { printf '\n==> %s\n' "$*"; }
project_number="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')"

say "Enabling APIs"
gcloud services enable iamcredentials.googleapis.com sts.googleapis.com

say "Workload identity pool ${POOL}"
if ! gcloud iam workload-identity-pools describe "${POOL}" --location=global >/dev/null 2>&1; then
  gcloud iam workload-identity-pools create "${POOL}" --location=global --display-name="GitHub Actions"
fi

say "GitHub OIDC provider ${PROVIDER} (only ${GITHUB_REPO}, main branch)"
if gcloud iam workload-identity-pools providers describe "${PROVIDER}" --location=global --workload-identity-pool="${POOL}" >/dev/null 2>&1; then
  gcloud iam workload-identity-pools providers update-oidc "${PROVIDER}" \
    --location=global --workload-identity-pool="${POOL}" --attribute-condition="${CONDITION}"
else
  gcloud iam workload-identity-pools providers create-oidc "${PROVIDER}" \
    --location=global \
    --workload-identity-pool="${POOL}" \
    --display-name="GitHub" \
    --issuer-uri="https://token.actions.githubusercontent.com" \
    --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref" \
    --attribute-condition="${CONDITION}"
fi

say "Deploy service account ${DEPLOY_SA}"
if ! gcloud iam service-accounts describe "${DEPLOY_SA}" >/dev/null 2>&1; then
  gcloud iam service-accounts create "${DEPLOY_SA_NAME}" --display-name="CDN Manager deploys from GitHub"
fi
for role in roles/cloudbuild.builds.editor roles/serviceusage.serviceUsageConsumer roles/logging.viewer roles/run.viewer; do
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${DEPLOY_SA}" --role="${role}" --condition=None >/dev/null
done
gcloud iam service-accounts add-iam-policy-binding "${DEPLOY_SA}" \
  --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/projects/${project_number}/locations/global/workloadIdentityPools/${POOL}/attribute.repository/${GITHUB_REPO}" >/dev/null

say "Source staging bucket gs://${BUCKET}"
if ! gcloud storage buckets describe "gs://${BUCKET}" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://${BUCKET}" --location="${REGION}" --uniform-bucket-level-access
fi
build_sa="$(gcloud builds get-default-service-account --format='value(serviceAccountEmail)' | sed 's#.*/##')"
gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
  --member="serviceAccount:${DEPLOY_SA}" --role=roles/storage.admin >/dev/null
gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
  --member="serviceAccount:${build_sa}" --role=roles/storage.objectViewer >/dev/null
gcloud iam service-accounts add-iam-policy-binding "${build_sa}" \
  --member="serviceAccount:${DEPLOY_SA}" --role=roles/iam.serviceAccountUser >/dev/null

cat <<EOF

==> Done. In GitHub open ${GITHUB_REPO} -> Settings -> Secrets and variables -> Actions -> Variables,
    and add these four repository variables:

    GCP_PROJECT_ID    ${PROJECT_ID}
    GCP_REGION        ${REGION}
    GCP_WIF_PROVIDER  projects/${project_number}/locations/global/workloadIdentityPools/${POOL}/providers/${PROVIDER}
    GCP_DEPLOY_SA     ${DEPLOY_SA}

    Then push to main, or run the "Test and deploy" workflow from the Actions tab.
EOF

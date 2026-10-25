# Shared settings for setup.sh and deploy.sh. Override any value by exporting it first,
# for example: REGION=asia-south2 ./deploy/gcp/setup.sh

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${REGION:-asia-south1}"
SERVICE="${SERVICE:-cdnmanager}"
REPO="${REPO:-cdnmanager}"

SQL_INSTANCE="${SQL_INSTANCE:-cdnmanager-pg}"
SQL_TIER="${SQL_TIER:-db-custom-1-3840}"
# REGIONAL keeps a standby in a second zone (about twice the database cost).
SQL_AVAILABILITY="${SQL_AVAILABILITY:-ZONAL}"
DB_NAME="${DB_NAME:-cdnmanager}"
DB_USER="${DB_USER:-cdnmanager}"

ADMIN_USERNAME="${ADMIN_USERNAME:-admin}"
RUN_SA_NAME="${RUN_SA_NAME:-cdnmanager-run}"

MIN_INSTANCES="${MIN_INSTANCES:-1}"
MAX_INSTANCES="${MAX_INSTANCES:-4}"
CPU="${CPU:-1}"
MEMORY="${MEMORY:-1Gi}"

if [[ -z "${PROJECT_ID}" ]]; then
  echo "No project. Run: gcloud config set project YOUR_PROJECT_ID" >&2
  exit 1
fi

RUN_SA="${RUN_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
CONNECTION_NAME="${PROJECT_ID}:${REGION}:${SQL_INSTANCE}"
DB_PASSWORD_SECRET="${SERVICE}-db-password"
ADMIN_PASSWORD_SECRET="${SERVICE}-admin-password"

#!/usr/bin/env bash
# Provision the private, multi-user HEALER deployment. This script is
# intentionally idempotent where Google Cloud permits it.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_FILE="${1:-$ROOT_DIR/deploy/cloud-run.env}"

if [[ ! -f "$CONFIG_FILE" ]]; then
  echo "Missing $CONFIG_FILE. Copy deploy/cloud-run.env.example first." >&2
  exit 2
fi

# shellcheck disable=SC1090
source "$CONFIG_FILE"
: "${PROJECT_ID:?PROJECT_ID is required}"
: "${ALLOWED_USERS:?ALLOWED_USERS is required}"
: "${BUILDING_BLOCK_BUCKET:?BUILDING_BLOCK_BUCKET is required}"
: "${BUILDING_BLOCK_PREFIX:?BUILDING_BLOCK_PREFIX is required}"
: "${BUILDING_BLOCK_MOUNT_PATH:?BUILDING_BLOCK_MOUNT_PATH is required}"
: "${TASK_QUEUE_MAX_DISPATCHES_PER_SECOND:=13}"
: "${TASK_QUEUE_MAX_ATTEMPTS:=10}"

IMAGE="$REGION-docker.pkg.dev/$PROJECT_ID/$REPOSITORY/healer:$(git -C "$ROOT_DIR" rev-parse --short HEAD)-$(date -u +%Y%m%d%H%M%S)"
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
WEB_SA="$WEB_SERVICE_ACCOUNT@$PROJECT_ID.iam.gserviceaccount.com"
WORKER_SA="$WORKER_SERVICE_ACCOUNT@$PROJECT_ID.iam.gserviceaccount.com"
DISPATCHER_SA="$TASK_DISPATCHER_SERVICE_ACCOUNT@$PROJECT_ID.iam.gserviceaccount.com"

gcloud config set project "$PROJECT_ID"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  redis.googleapis.com secretmanager.googleapis.com iap.googleapis.com compute.googleapis.com cloudtasks.googleapis.com storage.googleapis.com

gcloud artifacts repositories describe "$REPOSITORY" --location="$REGION" >/dev/null 2>&1 || \
  gcloud artifacts repositories create "$REPOSITORY" --repository-format=docker --location="$REGION"

gcloud compute networks describe "$NETWORK" >/dev/null 2>&1 || \
  gcloud compute networks create "$NETWORK" --subnet-mode=custom
gcloud compute networks subnets describe "$SUBNET" --region="$REGION" >/dev/null 2>&1 || \
  gcloud compute networks subnets create "$SUBNET" --network="$NETWORK" --region="$REGION" --range="$SUBNET_RANGE"

gcloud iam service-accounts describe "$WEB_SA" >/dev/null 2>&1 || \
  gcloud iam service-accounts create "$WEB_SERVICE_ACCOUNT" --display-name="HEALER web service"
gcloud iam service-accounts describe "$WORKER_SA" >/dev/null 2>&1 || \
  gcloud iam service-accounts create "$WORKER_SERVICE_ACCOUNT" --display-name="HEALER task worker"
gcloud iam service-accounts describe "$DISPATCHER_SA" >/dev/null 2>&1 || \
  gcloud iam service-accounts create "$TASK_DISPATCHER_SERVICE_ACCOUNT" --display-name="HEALER Cloud Tasks dispatcher"

gcloud redis instances describe "$REDIS_INSTANCE" --region="$REGION" >/dev/null 2>&1 || \
  gcloud redis instances create "$REDIS_INSTANCE" --region="$REGION" --size=1 --tier=basic \
    --connect-mode=DIRECT_PEERING --network="$NETWORK" --reserved-ip-range="$REDIS_RANGE"
gcloud redis instances describe "$REDIS_INSTANCE" --region="$REGION" --format='value(state)' | grep -qx READY || {
  echo "Memorystore is still provisioning. Re-run this script after it is READY." >&2
  exit 1
}

REDIS_HOST="$(gcloud redis instances describe "$REDIS_INSTANCE" --region="$REGION" --format='value(host)')"
REDIS_PORT="$(gcloud redis instances describe "$REDIS_INSTANCE" --region="$REGION" --format='value(port)')"
gcloud secrets describe "$REDIS_SECRET" >/dev/null 2>&1 || gcloud secrets create "$REDIS_SECRET" --replication-policy=automatic
printf 'redis://%s:%s/0' "$REDIS_HOST" "$REDIS_PORT" | gcloud secrets versions add "$REDIS_SECRET" --data-file=-
for SERVICE_ACCOUNT in "$WEB_SA" "$WORKER_SA"; do
  gcloud secrets add-iam-policy-binding "$REDIS_SECRET" --member="serviceAccount:$SERVICE_ACCOUNT" \
    --role=roles/secretmanager.secretAccessor >/dev/null
  gcloud storage buckets add-iam-policy-binding "gs://$BUILDING_BLOCK_BUCKET" \
    --member="serviceAccount:$SERVICE_ACCOUNT" --role=roles/storage.objectViewer >/dev/null
done

# A public Git URL lets Cloud Build fetch the checked-in source directly on
# Google infrastructure, avoiding a local source-archive upload.  Keep the
# local mode as a useful fallback for private, unpushed work.
if [[ -n "${BUILD_SOURCE_REPOSITORY:-}" ]]; then
  gcloud builds submit "$BUILD_SOURCE_REPOSITORY" \
    --git-source-revision="${BUILD_SOURCE_REVISION:-main}" \
    --config="deploy/cloudbuild.yaml" --substitutions="_IMAGE=$IMAGE"
else
  gcloud builds submit "$ROOT_DIR" --config="$ROOT_DIR/deploy/cloudbuild.yaml" --substitutions="_IMAGE=$IMAGE"
fi

COMMON_ENV="HEALER_SERVER_MODE=true,HEALER_RESULT_TTL_SECONDS=7200,HEALER_LIMIT_MAX_EVALS=2000,HEALER_LIMIT_MAX_PRODUCTS=100,HEALER_LIMIT_MAX_TOTAL=500,HEALER_LIMIT_N_COMP=10,HEALER_LIMIT_RETRO_DEPTH=1,HEALER_TASK_DEADLINE_SECONDS=900"
BUILDING_BLOCK_ENV="HEALER_DATA_DIR=$BUILDING_BLOCK_MOUNT_PATH"
BUILDING_BLOCK_VOLUME="mount-path=$BUILDING_BLOCK_MOUNT_PATH,type=cloud-storage,bucket=$BUILDING_BLOCK_BUCKET,readonly=true,mount-options=only-dir=$BUILDING_BLOCK_PREFIX"
gcloud tasks queues describe "$TASK_QUEUE" --location="$REGION" >/dev/null 2>&1 || \
  gcloud tasks queues create "$TASK_QUEUE" --location="$REGION" --max-concurrent-dispatches="$TASK_QUEUE_MAX_CONCURRENT_DISPATCHES" --max-dispatches-per-second="$TASK_QUEUE_MAX_DISPATCHES_PER_SECOND" --max-attempts="$TASK_QUEUE_MAX_ATTEMPTS" --min-backoff=5s
gcloud tasks queues update "$TASK_QUEUE" --location="$REGION" \
  --max-concurrent-dispatches="$TASK_QUEUE_MAX_CONCURRENT_DISPATCHES" --max-dispatches-per-second="$TASK_QUEUE_MAX_DISPATCHES_PER_SECOND" --max-attempts="$TASK_QUEUE_MAX_ATTEMPTS"
gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$WEB_SA" --role=roles/cloudtasks.enqueuer >/dev/null
gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$WORKER_SA" --role=roles/cloudtasks.enqueuer >/dev/null
gcloud iam service-accounts add-iam-policy-binding "$DISPATCHER_SA" \
  --member="serviceAccount:$WEB_SA" --role=roles/iam.serviceAccountUser >/dev/null
gcloud iam service-accounts add-iam-policy-binding "$DISPATCHER_SA" \
  --member="serviceAccount:$WORKER_SA" --role=roles/iam.serviceAccountUser >/dev/null
gcloud iam service-accounts add-iam-policy-binding "$DISPATCHER_SA" \
  --member="serviceAccount:service-$PROJECT_NUMBER@gcp-sa-cloudtasks.iam.gserviceaccount.com" --role=roles/iam.serviceAccountUser >/dev/null

gcloud run deploy "$TASK_WORKER_SERVICE_NAME" --image="$IMAGE" --region="$REGION" --no-allow-unauthenticated \
  --service-account="$WORKER_SA" --network="$NETWORK" --subnet="$SUBNET" --vpc-egress=private-ranges-only \
  --cpu="$TASK_WORKER_CPU" --memory="$TASK_WORKER_MEMORY" --concurrency=1 --max-instances="$TASK_WORKER_MAX_INSTANCES" --timeout="$TASK_WORKER_TIMEOUT" \
  --add-volume="$BUILDING_BLOCK_VOLUME" --set-env-vars="$COMMON_ENV,$BUILDING_BLOCK_ENV" \
  --set-secrets="HEALER_REDIS_URL=$REDIS_SECRET:latest"
TASK_WORKER_URL="$(gcloud run services describe "$TASK_WORKER_SERVICE_NAME" --region="$REGION" --format='value(status.url)')"
gcloud run services update "$TASK_WORKER_SERVICE_NAME" --region="$REGION" \
  --set-env-vars="HEALER_GCP_PROJECT=$PROJECT_ID,HEALER_TASKS_LOCATION=$REGION,HEALER_TASKS_QUEUE=$TASK_QUEUE,HEALER_TASK_WORKER_URL=$TASK_WORKER_URL,HEALER_TASK_DISPATCHER_SERVICE_ACCOUNT=$DISPATCHER_SA"
gcloud run services add-iam-policy-binding "$TASK_WORKER_SERVICE_NAME" --region="$REGION" \
  --member="serviceAccount:$DISPATCHER_SA" --role=roles/run.invoker >/dev/null

gcloud run deploy "$SERVICE_NAME" --image="$IMAGE" --region="$REGION" --no-allow-unauthenticated --iap \
  --service-account="$WEB_SA" --network="$NETWORK" --subnet="$SUBNET" --vpc-egress=private-ranges-only \
  --cpu=2 --memory=4Gi --concurrency=1 --max-instances=4 --timeout=60 --add-volume="$BUILDING_BLOCK_VOLUME" \
  --set-env-vars="$COMMON_ENV,$BUILDING_BLOCK_ENV,HEALER_GCP_PROJECT=$PROJECT_ID,HEALER_TASKS_LOCATION=$REGION,HEALER_TASKS_QUEUE=$TASK_QUEUE,HEALER_TASK_WORKER_URL=$TASK_WORKER_URL,HEALER_TASK_DISPATCHER_SERVICE_ACCOUNT=$DISPATCHER_SA" \
  --startup-probe=httpGet.path=/api/health,httpGet.port=8080,timeoutSeconds=10,periodSeconds=10,failureThreshold=3 \
  --set-secrets="HEALER_REDIS_URL=$REDIS_SECRET:latest"

gcloud beta services identity create --service=iap.googleapis.com --project="$PROJECT_ID" >/dev/null
gcloud run services add-iam-policy-binding "$SERVICE_NAME" --region="$REGION" \
  --member="serviceAccount:service-$PROJECT_NUMBER@gcp-sa-iap.iam.gserviceaccount.com" --role=roles/run.invoker >/dev/null

echo "IAP is enabled. For projects without a Google Cloud organization, complete the one-time custom OAuth client setup in the Cloud Run console before opening the service."

IFS=',' read -r -a USERS <<< "$ALLOWED_USERS"
for USER_EMAIL in "${USERS[@]}"; do
  gcloud iap web add-iam-policy-binding --member="user:$USER_EMAIL" --role=roles/iap.httpsResourceAccessor \
    --region="$REGION" --resource-type=cloud-run --service="$SERVICE_NAME"
done

gcloud run services describe "$SERVICE_NAME" --region="$REGION" --format='value(status.url)'

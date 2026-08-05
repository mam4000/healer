#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-bcquery}"
REGION="${REGION:-us-west1}"
BUCKET="${BUCKET:-healer_data}"

# These are the paths created when the prior job extracted the Molport ZIP.
INPUT_PREFIX="${INPUT_PREFIX:-Molport_Full_Database/All Stock Compounds}"
OUTPUT_PREFIX="${OUTPUT_PREFIX:-buildingblocks/Molport_Full_Database}"

JOB_NAME="${JOB_NAME:-healer-preprocess-molport}"
SERVICE_ACCOUNT="${SERVICE_ACCOUNT:-healer-preprocess@bcquery.iam.gserviceaccount.com}"
WORKERS="${WORKERS:-4}"

# Safety default: test one 500k-compound shard first.
# Set MAX_SHARDS=0 to process every shard after validation.
MAX_SHARDS="${MAX_SHARDS:-1}"

IMAGE="$(gcloud run services describe healer-web \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --format='value(spec.template.spec.containers[0].image)')"

gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" \
  --member="serviceAccount:$SERVICE_ACCOUNT" \
  --role="roles/storage.objectUser" >/dev/null

JOB_SCRIPT='
set -eu
work_dir=/tmp/molport
archive=$work_dir/$INPUT_FILENAME
sdf_file=${archive%.gz}

rm -rf "$work_dir"
mkdir -p "$work_dir" "/mnt/catalog/$OUTPUT_PREFIX"

cp "/mnt/catalog/$INPUT_OBJECT" "$archive"
gzip -dc "$archive" > "$sdf_file"

preprocess-bb "$sdf_file" \
  --output-dir "/mnt/catalog/$OUTPUT_PREFIX" \
  --workers "$WORKERS" \
  --verbose

rm -rf "$work_dir"
'

job_exists=false
if gcloud run jobs describe "$JOB_NAME" \
  --project="$PROJECT_ID" --region="$REGION" >/dev/null 2>&1; then
  job_exists=true
fi

count=0
skipped=0
while IFS= read -r source_uri; do
  [[ -z "$source_uri" ]] && continue

  input_object="${source_uri#gs://$BUCKET/}"
  source_filename="$(basename "$input_object")"
  processed_filename="${source_filename%.gz}"
  processed_filename="${processed_filename%.sdf}_processed.sdf"
  processed_uri="gs://$BUCKET/$OUTPUT_PREFIX/$processed_filename"

  # A completed Cloud Storage FUSE write appears as the final object. Never
  # redo a shard that already has a processed output file.
  if gcloud storage ls "$processed_uri" >/dev/null 2>&1; then
    echo "Skipping completed shard: $processed_uri"
    skipped=$((skipped + 1))
    continue
  fi

  if [[ "$MAX_SHARDS" != "0" && "$count" -ge "$MAX_SHARDS" ]]; then
    break
  fi

  echo "Processing: gs://$BUCKET/$input_object"

  common_args=(
    --project="$PROJECT_ID"
    --region="$REGION"
    --image="$IMAGE"
    --service-account="$SERVICE_ACCOUNT"
    --cpu=4
    --memory=8Gi
    --task-timeout=1h
    --max-retries=0
    --command=sh
    --args="-ceu,$JOB_SCRIPT"
    --update-env-vars="INPUT_OBJECT=$input_object,INPUT_FILENAME=$source_filename,OUTPUT_PREFIX=$OUTPUT_PREFIX,WORKERS=$WORKERS"
  )

  if [[ "$job_exists" == false ]]; then
    gcloud run jobs create "$JOB_NAME" \
      "${common_args[@]}" \
      --add-volume="mount-path=/mnt/catalog,type=cloud-storage,bucket=$BUCKET"
    job_exists=true
  else
    gcloud run jobs update "$JOB_NAME" "${common_args[@]}"
  fi

  gcloud run jobs execute "$JOB_NAME" \
    --project="$PROJECT_ID" \
    --region="$REGION" \
    --wait

  count=$((count + 1))
done < <(
  gcloud storage ls --recursive "gs://$BUCKET/$INPUT_PREFIX/" |
    awk '/\.sdf\.gz$/'
)

echo "Processed $count Molport shard(s)."
echo "Skipped $skipped previously processed shard(s)."
echo "Output: gs://$BUCKET/$OUTPUT_PREFIX/"

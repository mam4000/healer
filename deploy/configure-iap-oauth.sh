#!/usr/bin/env bash
# Attach a downloaded Google OAuth web-client JSON file to the HEALER IAP service.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_FILE="${2:-$ROOT_DIR/deploy/cloud-run.env}"
CREDENTIAL_FILE="${1:?Usage: $0 /path/to/client_secret.json [deploy/cloud-run.env]}"

[[ -f "$CREDENTIAL_FILE" ]] || { echo "OAuth credential file not found: $CREDENTIAL_FILE" >&2; exit 2; }
[[ -f "$CONFIG_FILE" ]] || { echo "Deployment configuration not found: $CONFIG_FILE" >&2; exit 2; }
command -v jq >/dev/null || { echo "jq is required." >&2; exit 2; }

# shellcheck disable=SC1090
source "$CONFIG_FILE"
: "${PROJECT_ID:?PROJECT_ID is required}"
: "${REGION:?REGION is required}"
: "${SERVICE_NAME:?SERVICE_NAME is required}"

CLIENT_ID="$(jq -r '.web.client_id // empty' "$CREDENTIAL_FILE")"
CLIENT_SECRET="$(jq -r '.web.client_secret // empty' "$CREDENTIAL_FILE")"
[[ -n "$CLIENT_ID" && -n "$CLIENT_SECRET" ]] || {
  echo "The file does not contain a Web OAuth client ID and secret." >&2
  exit 2
}

SETTINGS_FILE="$(mktemp)"
chmod 600 "$SETTINGS_FILE"
trap 'rm -f "$SETTINGS_FILE"' EXIT

jq -n --arg client_id "$CLIENT_ID" --arg client_secret "$CLIENT_SECRET" \
  '{accessSettings: {oauthSettings: {clientId: $client_id, clientSecret: $client_secret}}}' > "$SETTINGS_FILE"

gcloud iap settings set "$SETTINGS_FILE" \
  --project="$PROJECT_ID" --resource-type=cloud-run --region="$REGION" --service="$SERVICE_NAME"

echo "Custom OAuth client attached to IAP for $SERVICE_NAME. The temporary settings file was removed."

#!/bin/bash

# Configuration
PROJECT_ID="premium-gear-486210-f2"
REGION="europe-west10"
FUNCTION_NAME="log-intelligence"
ENTRY_POINT="log_intelligence_webhook"
RUNTIME="python311"
STATE_BUCKET="${STATE_BUCKET:-$PROJECT_ID-log-intelligence-state}"
LOG_RETENTION_DAYS="${LOG_RETENTION_DAYS:-90}"
# Cloud Scheduler is not available in europe-west10 (Berlin); the job's
# region is irrelevant for an HTTP target, so it lives in Frankfurt.
SCHEDULER_REGION="${SCHEDULER_REGION:-europe-west3}"

# Secrets come from the environment or a local .env (gitignored) — never
# hardcode them here; GitHub push protection blocks the push.
if [ -f .env ]; then
  set -a
  source .env
  set +a
fi
: "${CLAUDE_API_KEY:?Set CLAUDE_API_KEY in the environment or .env}"
: "${CHAT_WEBHOOK_URL:?Set CHAT_WEBHOOK_URL in the environment or .env}"
: "${ANALYZE_TOKEN:?Set ANALYZE_TOKEN in .env (e.g. openssl rand -hex 24) — shared secret for the Scheduler trigger}"

# Chat token audience = project number (Google signs Chat events with it).
PROJECT_NUMBER=$(gcloud projects describe $PROJECT_ID --format="value(projectNumber)")

# One-time infrastructure: ./deploy.sh --setup
# Creates the state/archive bucket, the lifecycle rule that rotates the raw
# log archive (deletes logs/ objects after $LOG_RETENTION_DAYS days; state/
# is untouched), and the hourly Cloud Scheduler trigger.
if [ "$1" = "--setup" ]; then
  echo "🪣 Creating bucket gs://$STATE_BUCKET (if missing)..."
  gcloud storage buckets create "gs://$STATE_BUCKET" \
    --project=$PROJECT_ID --location=$REGION 2>/dev/null || true

  echo "♻️  Setting lifecycle rule: delete logs/* after $LOG_RETENTION_DAYS days..."
  LIFECYCLE_FILE=$(mktemp)
  cat > "$LIFECYCLE_FILE" <<EOF
{
  "rule": [
    {
      "action": {"type": "Delete"},
      "condition": {"age": $LOG_RETENTION_DAYS, "matchesPrefix": ["logs/"]}
    }
  ]
}
EOF
  gcloud storage buckets update "gs://$STATE_BUCKET" \
    --project=$PROJECT_ID --lifecycle-file="$LIFECYCLE_FILE"
  rm -f "$LIFECYCLE_FILE"

  echo "⏰ Creating hourly Cloud Scheduler job (POST without Chat payload = scheduled mode)..."
  FUNCTION_URL=$(gcloud functions describe $FUNCTION_NAME --project=$PROJECT_ID \
    --region=$REGION --gen2 --format="value(serviceConfig.uri)" 2>/dev/null)
  if [ -n "$FUNCTION_URL" ]; then
    SCHEDULER_BODY="{\"token\": \"$ANALYZE_TOKEN\"}"
    gcloud scheduler jobs create http log-intelligence-hourly \
      --project=$PROJECT_ID --location=$SCHEDULER_REGION \
      --schedule="0 * * * *" --uri="$FUNCTION_URL" \
      --http-method=POST --message-body="$SCHEDULER_BODY" 2>/dev/null \
      || gcloud scheduler jobs update http log-intelligence-hourly \
        --project=$PROJECT_ID --location=$SCHEDULER_REGION \
        --schedule="0 * * * *" --uri="$FUNCTION_URL" \
        --http-method=POST --message-body="$SCHEDULER_BODY"
  else
    echo "   ⚠️ Function not deployed yet — run ./deploy.sh first, then --setup again."
  fi
  echo "✅ Setup finished."
  exit 0
fi

echo "🚀 Deploying $FUNCTION_NAME to $REGION in project $PROJECT_ID..."

gcloud functions deploy $FUNCTION_NAME \
  --project=$PROJECT_ID \
  --gen2 \
  --runtime=$RUNTIME \
  --region=$REGION \
  --trigger-http \
  --entry-point=$ENTRY_POINT \
  --set-env-vars "CLAUDE_API_KEY=$CLAUDE_API_KEY,CHAT_WEBHOOK_URL=$CHAT_WEBHOOK_URL,STATE_BUCKET=$STATE_BUCKET,CHAT_AUDIENCE=$PROJECT_NUMBER,ANALYZE_TOKEN=$ANALYZE_TOKEN" \
  --timeout=300 \
  --allow-unauthenticated

echo "✅ Deployment attempt finished."

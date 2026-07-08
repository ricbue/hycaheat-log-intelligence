#!/bin/bash

# Configuration
PROJECT_ID="premium-gear-486210-f2"
REGION="europe-west10"
FUNCTION_NAME="log-intelligence"
ENTRY_POINT="log_intelligence_webhook"
RUNTIME="python311"

# Secrets come from the environment or a local .env (gitignored) — never
# hardcode them here; GitHub push protection blocks the push.
if [ -f .env ]; then
  set -a
  source .env
  set +a
fi
: "${CLAUDE_API_KEY:?Set CLAUDE_API_KEY in the environment or .env}"
: "${CHAT_WEBHOOK_URL:?Set CHAT_WEBHOOK_URL in the environment or .env}"

echo "🚀 Deploying $FUNCTION_NAME to $REGION in project $PROJECT_ID..."

gcloud functions deploy $FUNCTION_NAME \
  --project=$PROJECT_ID \
  --gen2 \
  --runtime=$RUNTIME \
  --region=$REGION \
  --trigger-http \
  --entry-point=$ENTRY_POINT \
  --set-env-vars "CLAUDE_API_KEY=$CLAUDE_API_KEY,CHAT_WEBHOOK_URL=$CHAT_WEBHOOK_URL" \
  --allow-unauthenticated

echo "✅ Deployment attempt finished."

# Log Intelligence for Hyca Heat

Automated monitoring and analysis of the Cloud Run request logs (hycaheat.com
& friends) using the Claude API — plus an interactive Google Chat bot to ask
questions about the logs.

## Features

- **LLM bot tracking:** GPTBot, ClaudeBot, PerplexityBot, Googlebot, …
- **Chat with your logs:** mention the bot in Google Chat ("welche AI-Crawler
  waren heute da?") — answers from the last 24 h of request logs.
- **Standing instructions:** messages containing "ignore", "remember" or
  "normal" are stored and injected into future scheduled analyses.
- **Scheduled anomaly reports:** hourly Cloud Scheduler run per service;
  posts to Google Chat only when noteworthy.
- **Raw-log archive with rotation:** every scheduled run appends the batch as
  JSONL to `gs://<bucket>/logs/<service>/YYYY/MM/DD/HHMMSS.jsonl`; a GCS
  lifecycle rule deletes archive objects after 90 days (configurable).
- **State in GCS:** baseline summaries, cursors and instructions live in
  `gs://<bucket>/intelligence_state.json` — Cloud Function filesystems are
  ephemeral.

## Deployment

```bash
./deploy.sh           # deploy the Cloud Function (reads secrets from .env)
./deploy.sh --setup   # one-time: bucket, lifecycle rotation, hourly scheduler
```

Secrets live in a local `.env` (gitignored, see `.env` keys below) — never in
the scripts; GitHub push protection blocks pushes containing keys.

### Environment variables

- `CLAUDE_API_KEY` — Anthropic API key
- `CHAT_WEBHOOK_URL` — Google Chat incoming webhook (scheduled reports)
- `STATE_BUCKET` — GCS bucket for state + log archive (default: `<project>-log-intelligence-state`)
- `LOG_RETENTION_DAYS` — archive rotation, deploy-time only (default 90)

## Google Chat app (interactive bot)

The incoming webhook can only *post*; to make @LogBot answer, the function
must be registered as a Chat app:

1. GCP Console → **Google Chat API** → aktivieren → **Configuration**.
2. App name/avatar/description setzen, **Interactive features** an.
3. **Connection settings**: HTTP endpoint URL = URL der deployten Function
   (`gcloud functions describe log-intelligence --gen2 --region=europe-west10
   --format="value(serviceConfig.uri)"`).
4. **Visibility**: eigene Domain/Nutzer freigeben, speichern.
5. Im Chat-Space: Apps hinzufügen → LogBot → mit `@LogBot <Frage>` testen.

⚠️ Prototype caveat: the function is `--allow-unauthenticated` and does not
verify Google Chat bearer tokens — anyone with the URL can query log summaries
and spend Claude tokens. For hardening, verify the `Authorization: Bearer`
token against issuer `chat@system.gserviceaccount.com` (audience = project
number) and put a shared secret on the Scheduler payload.

## Local usage

```bash
pip install -r requirements.txt
python -m functions_framework --target log_intelligence_webhook  # serve locally
```

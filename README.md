# Log Intelligence for Hyca Heat

Automated monitoring and analysis of the Cloud Run request logs (hycaheat.com
& friends) using the Claude API — plus an interactive Google Chat bot to ask
questions about the logs.

## Features

- **LLM bot tracking:** GPTBot, ClaudeBot, PerplexityBot, Googlebot, …
- **Chat with your logs:** mention the bot in Google Chat ("welche AI-Crawler
  waren heute da?") — answers from the last 24 h of request logs.
- **Slash commands** (everything else is treated as a chat question):
  `/remember <text>` stores a standing instruction for future analyses,
  `/analyze` triggers the Scheduler job (report arrives via webhook),
  `/status` shows cursors and instruction counts without a Claude call.
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
./deploy.sh             # deploy the Cloud Function (reads secrets from .env)
./deploy.sh --setup     # one-time: bucket, lifecycle rotation, hourly scheduler
./deploy.sh --snapshot  # copy the live GCS state into the repo (commit manually)
```

Secrets live in a local `.env` (gitignored, see `.env` keys below) — never in
the scripts; GitHub push protection blocks pushes containing keys.

### Environment variables

- `CLAUDE_API_KEY` — Anthropic API key
- `CHAT_WEBHOOK_URL` — Google Chat incoming webhook (scheduled reports)
- `ANALYZE_TOKEN` — shared secret for the Scheduler trigger (`openssl rand -hex 24`)
- `STATE_BUCKET` — GCS bucket for state + log archive (default: `<project>-log-intelligence-state`)
- `LOG_RETENTION_DAYS` — archive rotation, deploy-time only (default 90)
- `CHAT_AUDIENCE` — set automatically by deploy.sh (project number); enables
  verification of the Google-signed bearer token on Chat events

## Google Chat app (interactive bot)

The incoming webhook can only *post*; to make @LogBot answer, the function
must be registered as a Chat app:

1. GCP Console → **Google Chat API** → aktivieren → **Configuration**.
2. App name/avatar/description setzen, **Interactive features** an.
3. **Connection settings**: HTTP endpoint URL = URL der deployten Function
   (`gcloud functions describe log-intelligence --gen2 --region=europe-west10
   --format="value(serviceConfig.uri)"`).
4. **Visibility**: eigene Domain/Nutzer freigeben, speichern.
5. **Commands** (im Abschnitt "Commands"/"Slash commands" der Konfiguration) —
   die IDs müssen zu `main.py` (CMD_*) passen:
   | ID | Name | Beschreibung |
   |---|---|---|
   | 1 | `/remember` | Dauerhafte Anweisung für die Analysen speichern |
   | 2 | `/analyze` | Analyse-Lauf sofort starten (Bericht kommt per Webhook) |
   | 3 | `/status` | Cursor & Anweisungen pro Service anzeigen |
6. Im Chat-Space: Apps hinzufügen → LogBot → mit `@LogBot <Frage>` testen.

Die Function versteht beide Event-Formate: das Legacy-Format (`type:
MESSAGE`) und das neue Add-on-Format (`chat.messagePayload` /
`chat.appCommandPayload`), das die aktuelle Console-Konfiguration
("common HTTP endpoint URL" bzw. per-Trigger-URLs) sendet.

The endpoint is `--allow-unauthenticated` at the HTTP level, but hardened in
code: Chat events must carry a valid Google-signed bearer token (issuer
`chat@system.gserviceaccount.com`, audience = project number via
`CHAT_AUDIENCE`), and the Scheduler trigger must send the shared
`ANALYZE_TOKEN`. Requests without either are rejected (401/403).

## Local usage

```bash
pip install -r requirements.txt
python -m functions_framework --target log_intelligence_webhook  # serve locally
```

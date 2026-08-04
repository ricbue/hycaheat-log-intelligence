# Log Intelligence for Hyca Heat

Automated monitoring and analysis of the Cloud Run request logs using the
Claude API — plus an interactive Google Chat bot to ask questions about the
logs. Monitored services: hycaheat-website-prod and
hycaheat-configurator-prod (project premium-gear-486210-f2) plus the TCO
simulator tco.hyca.app (tco-frontend-prod / tco-backend-prod, read
cross-project from GCP project `hyboid`).

**[ARCHITECTURE.md](ARCHITECTURE.md)** describes the whole implementation —
components, data flow, state design, and the design principles behind the
alerting — and is written to be shareable on its own.

## Features

- **LLM bot tracking:** GPTBot, ClaudeBot, PerplexityBot, Googlebot, …
- **Chat with your logs:** mention the bot in Google Chat ("welche AI-Crawler
  waren heute da?") — answers from the last 24 h of raw request logs, per-day
  aggregates (~3 weeks, for trend questions like "wie war die Bot-Aktivität
  die letzten Wochen?") and the archived weekly digests.
- **Slash commands** (everything else is treated as a chat question):
  `/remember <text>` stores a standing instruction for future analyses
  (persisted in the GCS state; unlike baselines, never rewritten by the
  model — e.g. the known-benign list of scanner patterns),
  `/analyze` triggers the Scheduler job (report arrives via webhook),
  `/status` shows cursors and instruction counts without a Claude call.
- **Hourly anomaly checks:** Cloud Scheduler run per service; posts to
  Google Chat ONLY when immediate action is required (service down,
  sustained user-facing 5xx, an attack actually succeeding). Everything
  else — new crawlers, 404s, traffic shifts, scanner noise — accumulates
  in the baselines for the weekly digest.
- **Weekly digest:** Mondays 07:00 Europe/Berlin, posted unconditionally
  (in German) — traffic level & trend, error picture, crawler/GEO activity
  per service, aggregated from the full raw-log archive rather than the
  500-entry live-query cap.
- **Raw-log archive with rotation:** every scheduled run appends the batch as
  JSONL to `gs://<bucket>/logs/<service>/YYYY/MM/DD/HHMMSS.jsonl`; a GCS
  lifecycle rule deletes archive objects after 90 days (configurable).
- **Weekly digest archive:** every Monday digest is also stored as
  `gs://<bucket>/digests/YYYY-MM-DD.md` (kept forever, unlike the raw logs) —
  the long-term record of how traffic and error patterns develop.
- **State in GCS:** baseline summaries, cursors and instructions live in
  `gs://<bucket>/intelligence_state.json` — Cloud Function filesystems are
  ephemeral.

## Deployment

```bash
./deploy.sh             # deploy the Cloud Function (reads secrets from .env)
./deploy.sh --setup     # one-time: bucket, lifecycle rule, hourly + weekly
                        # scheduler jobs, IAM (jobRunner for /analyze,
                        # cross-project logging.viewer on hyboid)
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
- `ANALYZE_JOB` — set automatically by deploy.sh; full resource name of the
  hourly Scheduler job that `/analyze` triggers

## Google Chat channel setup

Fair warning: this was by far the most painful part of the whole setup.
The channel consists of **two separate identities** that look like one bot
but are configured in completely different places:

1. **Incoming webhook** (posts the scheduled reports & digests): created in
   the Chat space itself — space name → *Apps & integrations* → *Webhooks*.
   The generated URL is `CHAT_WEBHOOK_URL`. A webhook can only *post*; it
   can never read or answer anything.
2. **Chat app** ("@LogBot", answers questions): registered via the Google
   Chat API configuration in the GCP console (below). It receives events at
   the function URL and replies synchronously.

### Setup gotchas (each of these cost real time)

- **Webhook ≠ app.** They appear as two different members in the space.
  Nothing you configure on the webhook affects the app or vice versa — if
  scheduled reports arrive but mentions go unanswered (or the reverse),
  you're debugging the wrong identity.
- **Two event formats.** The current console configuration ("HTTP endpoint
  URL") delivers events in the new **add-on format** (`chat.messagePayload`,
  User-Agent `Google-gsuiteaddons`), not the legacy format (`type: MESSAGE`)
  most documentation and examples show. `main.py` parses both
  (`parse_chat_event`), and replies must also match the format
  (`chat_reply`: `hostAppDataAction` vs. plain `{"text": ...}`).
- **Token verification differs per format.** Legacy events are signed by
  `chat@system.gserviceaccount.com` with audience = project *number*;
  add-on events are signed with Google's standard OIDC keys, audience =
  the **endpoint URL**, sender
  `service-<project-number>@gcp-sa-gsuiteaddons.iam.gserviceaccount.com`.
  Verifying only the legacy way silently 401s every real event.
- **Errors hide in oddly-named logs.** Delivery failures land in Cloud
  Logging under `chat.googleapis.com%2Ferrors` and
  `gsuiteaddons.googleapis.com%2Ferrors` — and only if "Log errors to
  Logging" is enabled in the Chat API configuration. Without it, failures
  are completely invisible.
- **The app is findable only if "Join spaces" is enabled** in the app's
  visibility settings, and the space search needs the literal `@` prefix
  ("@logbot"), not just the name.
- **Log ingestion lags minutes.** "No logs for my test message" does not
  mean the event never arrived — wait before concluding the config is
  broken.
- **Slash-command IDs are matched by number**, and they arrive as JSON
  floats (`3.0`), not strings — the IDs configured in the console must
  match `CMD_*` in `main.py`, which normalizes the type.

### Registering the Chat app

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

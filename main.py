import os
import json
from datetime import datetime, timedelta, timezone
import requests
from google.auth.transport import requests as google_auth_requests
from google.cloud import logging as cloud_logging
from google.cloud import storage
from google.oauth2 import id_token
import functions_framework

# Configuration
PROJECT_ID = os.getenv("PROJECT_ID", "premium-gear-486210-f2")
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY")
CHAT_WEBHOOK_URL = os.getenv("CHAT_WEBHOOK_URL")
STATE_BUCKET = os.getenv("STATE_BUCKET", f"{PROJECT_ID}-log-intelligence-state")
STATE_FILE = "intelligence_state.json"
# Project *number* — audience of the Google-signed bearer token on Chat
# events. Unset = verification skipped (local testing only).
CHAT_AUDIENCE = os.getenv("CHAT_AUDIENCE")
# Shared secret the Cloud Scheduler job sends as {"token": ...}; the
# function endpoint itself is public (--allow-unauthenticated).
ANALYZE_TOKEN = os.getenv("ANALYZE_TOKEN")
# Full resource name of the hourly Scheduler job; /analyze triggers it.
ANALYZE_JOB = os.getenv("ANALYZE_JOB", f"projects/{PROJECT_ID}/locations/europe-west3/jobs/log-intelligence-hourly")

CHAT_ISSUER = "chat@system.gserviceaccount.com"
CHAT_CERTS_URL = f"https://www.googleapis.com/service_accounts/v1/metadata/x509/{CHAT_ISSUER}"

# service -> GCP project hosting it. The TCO sim (tco.hyca.app) moved to
# project "hyboid"; the identically named tco-* services in the main
# project were shut down.
SERVICES = {
    "hycaheat-website-prod": PROJECT_ID,
    "hycaheat-configurator-prod": PROJECT_ID,
    "tco-frontend-prod": "hyboid",
    "tco-backend-prod": "hyboid",
}
LLM_BOTS = ["GPTBot", "ClaudeBot", "Googlebot", "CCBot", "PerplexityBot", "OAI-SearchBot", "Applebot", "Bytespider",
            # user-triggered AI fetchers — an AI product retrieving a page for a live answer
            "ChatGPT-User", "Claude-User", "Claude-Web", "GoogleAgent-URLContext", "Perplexity-User", "Meta-ExternalAgent", "DuckAssistBot"]
# GEO files whose access we track per user agent (only served by the website)
GEO_FILES = ["llms.txt", "facts.md"]

def load_state():
    try:
        client = storage.Client(project=PROJECT_ID)
        bucket = client.bucket(STATE_BUCKET)
        blob = bucket.blob(STATE_FILE)
        if blob.exists():
            return json.loads(blob.download_as_text())
    except Exception as e:
        print(f"Error loading state from GCS: {e}")
    return {"services": {service: {"last_timestamp": None, "baseline_summary": "No baseline established yet.", "human_instructions": ""} for service in SERVICES}}

def save_state(state):
    try:
        client = storage.Client(project=PROJECT_ID)
        bucket = client.lookup_bucket(STATE_BUCKET)
        if not bucket:
            bucket = client.create_bucket(STATE_BUCKET, location="europe-west10")
        blob = bucket.blob(STATE_FILE)
        blob.upload_from_string(json.dumps(state, indent=2))
    except Exception as e:
        print(f"Error saving state to GCS: {e}")

def get_logs(service_name, lookback_hours=24, start_from=None):
    client = cloud_logging.Client(project=SERVICES.get(service_name, PROJECT_ID))
    if start_from:
        try:
            start_time = datetime.fromisoformat(start_from.replace("Z", "+00:00"))
        except:
            start_time = (datetime.now(timezone.utc) - timedelta(hours=lookback_hours))
    else:
        start_time = (datetime.now(timezone.utc) - timedelta(hours=lookback_hours))

    # logName restricted to request logs: without it the query also returns
    # container stdout/stderr lines, which have no httpRequest and show up
    # as bogus null-status entries (50 % of the first report's sample).
    filter_str = (
        f'resource.type="cloud_run_revision" AND resource.labels.service_name="{service_name}" '
        f'AND logName:"logs/run.googleapis.com%2Frequests" '
        f'AND timestamp >= "{start_time.strftime("%Y-%m-%dT%H:%M:%SZ")}"'
    )
    # DESCENDING + reverse: with the default ascending order, max_results
    # returns the *oldest* entries of the window instead of the newest.
    entries = client.list_entries(filter_=filter_str, order_by=cloud_logging.DESCENDING, max_results=500)

    logs, latest_ts = [], start_from
    for entry in entries:
        ts = entry.timestamp.isoformat() if entry.timestamp else None
        if ts and (not latest_ts or ts > latest_ts): latest_ts = ts
        logs.append({"timestamp": ts, "httpRequest": entry.http_request if entry.http_request else {}, "resource": entry.resource.labels if entry.resource else {}})
    logs.reverse()  # chronological for archive + prompt
    return logs, latest_ts

def archive_logs(service_name, logs):
    """Append the batch to the raw-log archive: gs://<bucket>/logs/<service>/YYYY/MM/DD/HHMMSS.jsonl.

    Rotation happens via the bucket lifecycle rule on the logs/ prefix
    (deploy.sh --setup, default 90 days) — no in-code cleanup needed.
    """
    if not logs:
        return
    try:
        now = datetime.now(timezone.utc)
        path = f"logs/{service_name}/{now.strftime('%Y/%m/%d')}/{now.strftime('%H%M%S')}.jsonl"
        client = storage.Client(project=PROJECT_ID)
        bucket = client.lookup_bucket(STATE_BUCKET)
        if not bucket:
            bucket = client.create_bucket(STATE_BUCKET, location="europe-west10")
        bucket.blob(path).upload_from_string(
            "\n".join(json.dumps(e, default=str) for e in logs),
            content_type="application/jsonl",
        )
        print(f"Archived {len(logs)} entries to gs://{STATE_BUCKET}/{path}")
    except Exception as e:
        print(f"Error archiving logs to GCS: {e}")

def preprocess_logs(logs):
    processed = []
    stats = {"total_count": len(logs), "status_codes": {}, "bot_hits": {bot: 0 for bot in LLM_BOTS}}
    # A few example lines per status code: without them a ratio shift
    # (e.g. a 301 spike) shows up in STATS but can't be root-caused.
    status_samples = {}
    for entry in logs:
        hr = entry.get("httpRequest", {})
        status, ua, url = hr.get("status"), hr.get("userAgent", ""), hr.get("requestUrl", "")
        stats["status_codes"][status] = stats["status_codes"].get(status, 0) + 1
        is_bot = False
        for bot in LLM_BOTS:
            if bot.lower() in ua.lower():
                stats["bot_hits"][bot] += 1
                is_bot = True; break
        is_geo_file = any(gf in url for gf in GEO_FILES)
        status_samples[status] = status_samples.get(status, 0) + 1
        if is_bot or is_geo_file or (status and status >= 400) or status_samples[status] <= 3:
            # responseSize distinguishes a real file from the SPA fallback
            # document a catch-all serves for any probed path (200 or 206).
            processed.append({"t": entry.get("timestamp"), "s": status, "b": hr.get("responseSize"), "u": url, "ua": ua})
    return processed, stats

def analyze_with_claude(service_name, processed_logs, stats, service_state, user_message=None):
    mode = f"CHAT MODE. User said: '{user_message}'" if user_message else "SCHEDULED MODE. Perform anomaly check."
    prompt = f"""
    Analyze logs for '{service_name}'. 
    CONTEXT: {mode}
    BASELINE: {service_state['baseline_summary']}
    STANDING INSTRUCTIONS: {service_state.get('human_instructions', 'None')}
    STATS: {json.dumps(stats)}
    LOGS: {json.dumps(processed_logs[:150], indent=2)}
    
    DATA CONTRACT: STATS covers every request in the window. LOGS is a
    deliberately filtered sample — bot hits, GEO-file hits, all 4xx/5xx,
    plus a few example lines per status code. A small or empty LOGS despite
    a nonzero total_count is normal by design, never a defect or anomaly.
    Each LOGS line is {{t: timestamp, s: status, b: response bytes,
    u: URL, ua: user agent}}.

    TASK:
    1. If user sent a message, answer it using the logs. Be technical and detailed.
    2. 'noteworthy' is true ONLY if a human must act right now: service down
       or unreachable, users hit by sustained 5xx, or an attack that is
       actually succeeding (routine scanning/probing is background noise on
       any public site). A status code alone NEVER proves a probe succeeded:
       SPA/static frontends answer EVERY unknown path with the index.html
       fallback — 200, or 206 when the scanner sends a Range header. Claim
       success only with corroborating evidence: a non-HTML content type or
       a response size (b) clearly different from the fallback document.
       Test: name the concrete action the operator should take — if there
       is none, noteworthy is false. Volume swings, status-ratio shifts, new
       crawlers, recurring 404s and similar curiosities are NEVER
       noteworthy; they belong in the weekly digest.
    3. Update the baseline summary so the weekly digest can report the
       non-severe observations. Compact FACTS only (traffic patterns, known
       noise, open observations), max ~1500 characters. No policies,
       thresholds, escalation protocols, occurrence counters or cycle
       numbering — the alerting rules live in this prompt, not in the
       baseline.
    """
    headers = {"x-api-key": CLAUDE_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    data = {
        "model": "claude-sonnet-5",
        # 8192: adaptive thinking (default on this model) plus report and a
        # growing baseline share this budget — 2048 truncated the JSON mid-string.
        "max_tokens": 8192,
        # Structured output: the API guarantees the response is valid JSON
        # matching this schema — no text parsing, no fail-open fallback.
        "output_config": {
            "format": {
                "type": "json_schema",
                "schema": {
                    "type": "object",
                    "properties": {
                        "noteworthy": {"type": "boolean", "description": "True only if immediate human action is required (service down, sustained user-facing 5xx, attack actually succeeding)"},
                        "report": {"type": "string", "description": "Report/response for Google Chat. Answer the user directly if they asked."},
                        "updated_baseline": {"type": "string", "description": "Updated baseline summary — compact facts only, max ~1500 characters"},
                    },
                    "required": ["noteworthy", "report", "updated_baseline"],
                    "additionalProperties": False,
                },
            }
        },
        "messages": [{"role": "user", "content": prompt}],
    }
    response = requests.post("https://api.anthropic.com/v1/messages", headers=headers, json=data)
    res_json = response.json()
    full_text = "".join(b["text"] for b in res_json.get("content", []) if b["type"] == "text")
    try:
        result = json.loads(full_text)
        return bool(result["noteworthy"]), result["report"], result["updated_baseline"]
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        # Fail closed: severe-only alert channel — never spam the space on a
        # parse failure; keep the old baseline so nothing is lost.
        print(f"Unexpected Claude response for {service_name}: {e} | {full_text[:300]}")
        return False, full_text, service_state['baseline_summary']

def collect_weekly_stats(service_name, days=7):
    """Aggregate the raw-log archive (written by the hourly runs) for the
    last `days` days — full data, unlike the 500-entry live-query cap."""
    stats = {"total": 0, "daily": {}, "status_codes": {}, "bot_hits": {bot: 0 for bot in LLM_BOTS}, "error_paths": {}, "geo_file_hits": {}}
    try:
        client = storage.Client(project=PROJECT_ID)
        bucket = client.lookup_bucket(STATE_BUCKET)
        if not bucket:
            return stats
        today = datetime.now(timezone.utc).date()
        for d in range(days):
            day = today - timedelta(days=d)
            for blob in bucket.list_blobs(prefix=f"logs/{service_name}/{day.strftime('%Y/%m/%d')}/"):
                for line in blob.download_as_text().splitlines():
                    try:
                        hr = json.loads(line).get("httpRequest") or {}
                    except (json.JSONDecodeError, AttributeError):
                        continue
                    status, ua, url = hr.get("status"), hr.get("userAgent") or "", hr.get("requestUrl") or ""
                    stats["total"] += 1
                    stats["daily"][day.isoformat()] = stats["daily"].get(day.isoformat(), 0) + 1
                    stats["status_codes"][str(status)] = stats["status_codes"].get(str(status), 0) + 1
                    for bot in LLM_BOTS:
                        if bot.lower() in ua.lower():
                            stats["bot_hits"][bot] += 1; break
                    if status and status >= 400:
                        key = f"{status} {url.split('?')[0]}"
                        stats["error_paths"][key] = stats["error_paths"].get(key, 0) + 1
                    for gf in GEO_FILES:
                        if gf in url:
                            hits = stats["geo_file_hits"].setdefault(gf, {})
                            agent = (ua[:80] or "unknown")
                            hits[agent] = hits.get(agent, 0) + 1
                            break
    except Exception as e:
        print(f"Error collecting weekly stats for {service_name}: {e}")
    stats["error_paths"] = dict(sorted(stats["error_paths"].items(), key=lambda kv: -kv[1])[:15])
    stats["bot_hits"] = {b: c for b, c in stats["bot_hits"].items() if c}
    return stats

def weekly_digest(state):
    """Compose the weekly overview from archive stats + accumulated baselines
    and post it to the Chat space unconditionally."""
    sections = []
    for service in SERVICES:
        stats = collect_weekly_stats(service)
        baseline = state["services"].get(service, {}).get("baseline_summary", "No baseline.")
        sections.append(f"### {service}\nWEEKLY STATS (7 Tage, aus dem Log-Archiv): {json.dumps(stats)}\nBASELINE NOTES (von den stündlichen Checks gepflegt): {baseline}")
    prompt = (
        "You are LogBot, the log-analysis assistant for hycaheat.com and the "
        "TCO simulator tco.hyca.app (tco-frontend-prod / tco-backend-prod, "
        "GCP project 'hyboid').\n\n"
        + "\n\n".join(sections)
        + "\n\nSchreibe die Wochenübersicht für den Google-Chat-Space auf Deutsch "
        "(Plain Text, *fett* erlaubt, keine Markdown-Tabellen). Pro Service kurz: "
        "Traffic-Niveau und -Trend, Fehlerbild (welche 404/5xx relevant sind, was "
        "Rauschen ist), sonstige Auffälligkeiten. Kompakt und lesbar — ein Digest, "
        "kein Daten-Dump.\n"
        "Crawler-/Bot-Aktivität nur für hycaheat-website-prod berichten (für die "
        "anderen Services irrelevant). Dort einen eigenen GEO-Abschnitt: Zugriffe "
        "auf llms.txt und facts.md (geo_file_hits) mit User-Agents ausweisen und "
        "dabei echte AI-Crawler/-Agents (GPTBot, ClaudeBot, PerplexityBot, "
        "GoogleAgent-URLContext, ChatGPT-User …) klar von SEO-Scannern und "
        "Monitoring-Tools (BuiltWith, TheWebReport, PTST, curl …) trennen."
    )
    headers = {"x-api-key": CLAUDE_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    data = {"model": "claude-sonnet-5", "max_tokens": 2048, "messages": [{"role": "user", "content": prompt}]}
    response = requests.post("https://api.anthropic.com/v1/messages", headers=headers, json=data)
    digest = "".join(b["text"] for b in response.json().get("content", []) if b["type"] == "text")
    requests.post(CHAT_WEBHOOK_URL, json={"text": "📊 *Wochenübersicht Logs*\n\n" + digest})
    # Keep the digest beyond the Chat scroll-back: the raw-log archive
    # rotates after 90 days, so these are the long-term trend record. The
    # lifecycle rule only matches the logs/ prefix — digests/ is kept forever.
    try:
        client = storage.Client(project=PROJECT_ID)
        bucket = client.lookup_bucket(STATE_BUCKET)
        if bucket:
            path = f"digests/{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.md"
            bucket.blob(path).upload_from_string(digest, content_type="text/markdown")
            print(f"Archived weekly digest to gs://{STATE_BUCKET}/{path}")
    except Exception as e:
        print(f"Error archiving weekly digest: {e}")

def answer_chat(state, user_message):
    """Answer a Google Chat question with ONE Claude call across all services.

    Google Chat expects a synchronous reply within ~30 s; one call per
    service would blow that budget. Read-only: does not advance the
    scheduler cursor, archive logs, or rewrite baselines (except appending
    an explicit standing instruction).
    """
    sections = []
    for service in SERVICES:
        s_state = state["services"].get(service, {"baseline_summary": "No baseline established yet.", "human_instructions": ""})
        logs, _ = get_logs(service, lookback_hours=24)
        processed, stats = preprocess_logs(logs)
        sections.append(
            f"### Service: {service}\n"
            f"BASELINE: {s_state['baseline_summary']}\n"
            f"STANDING INSTRUCTIONS: {s_state.get('human_instructions') or 'None'}\n"
            f"STATS: {json.dumps(stats)}\n"
            f"LOG SAMPLE (bots, errors, GEO files + a few lines per status code): {json.dumps(processed[:60])}"
        )

    prompt = (
        "You are LogBot, the log-analysis assistant for hycaheat.com "
        "(Cloud Run services behind a GCP load balancer) and the TCO "
        "simulator at tco.hyca.app (tco-frontend-prod / tco-backend-prod, "
        "hosted in the separate GCP project 'hyboid').\n\n"
        + "\n\n".join(sections)
        + "\n\nWindow: last 24 hours, up to 500 most recent requests per service."
        + f"\n\nUSER QUESTION: '{user_message}'\n\n"
        "Answer the question directly and concisely for a Google Chat message "
        "(plain text, *bold* allowed, no markdown tables). Only mention services "
        "relevant to the question. Base claims strictly on the data above; say "
        "so when the window or sample does not contain the answer."
    )
    headers = {"x-api-key": CLAUDE_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    data = {"model": "claude-sonnet-5", "max_tokens": 1024, "messages": [{"role": "user", "content": prompt}]}
    response = requests.post("https://api.anthropic.com/v1/messages", headers=headers, json=data)
    res_json = response.json()
    answer = "".join(b["text"] for b in res_json.get("content", []) if b["type"] == "text")
    return answer or "Keine Antwort erhalten — bitte noch einmal versuchen."

# Slash commands — IDs must match the command configuration in the
# Google Chat API console. Everything else is treated as a chat question.
CMD_REMEMBER, CMD_ANALYZE, CMD_STATUS = "1", "2", "3"

def cmd_remember(state, args):
    if not args:
        return "Nutzung: /remember <Anweisung>, z. B. /remember 404s auf /apple-touch-icon* sind normal."
    for service in SERVICES:
        s_state = state["services"].setdefault(service, {"last_timestamp": None, "baseline_summary": "No baseline established yet.", "human_instructions": ""})
        s_state["human_instructions"] = (s_state.get("human_instructions") or "") + f"\n- {args}"
    save_state(state)
    return f"Gespeichert als dauerhafte Anweisung für alle Analysen:\n_{args}_"

def cmd_analyze():
    """Kick off the hourly Scheduler job instead of analyzing inline —
    a full run takes ~100 s, far beyond Chat's ~30 s reply window.
    The report arrives in the space via the incoming webhook."""
    try:
        from google.cloud import scheduler_v1
        scheduler_v1.CloudSchedulerClient().run_job(name=ANALYZE_JOB)
        return "Analyse gestartet — eine Meldung kommt nur bei gravierenden Problemen in den Space (Routine-Beobachtungen landen in der Wochenübersicht)."
    except Exception as e:
        print(f"cmd_analyze failed: {e}")
        return f"Konnte den Analyse-Job nicht starten: {e}"

def cmd_status(state):
    """Quick state dump without a Claude call."""
    lines = ["*LogBot-Status*"]
    for service in SERVICES:
        s = state["services"].get(service, {})
        cursor = s.get("last_timestamp") or "nie"
        n_instr = len([l for l in (s.get("human_instructions") or "").splitlines() if l.strip()])
        lines.append(f"- {service}: Cursor {cursor}, {n_instr} Anweisung(en)")
    return "\n".join(lines)

def _norm_cmd_id(raw):
    """JSON numbers arrive as floats — str(3.0) would never match '3'."""
    try:
        return str(int(float(raw)))
    except (TypeError, ValueError):
        return str(raw or "")

def parse_chat_event(body):
    """Normalize legacy events (type: MESSAGE, …) and new add-on-style
    events (chat.messagePayload, …) into (kind, text, command_id).

    kind: 'added' | 'message' | 'command' | 'other'
    """
    chat = body.get("chat")
    if chat is not None:  # new add-on format (per-trigger / common URL config)
        if "addedToSpacePayload" in chat:
            return "added", "", None
        if "appCommandPayload" in chat:
            payload = chat["appCommandPayload"]
            cmd = _norm_cmd_id(payload.get("appCommandMetadata", {}).get("appCommandId"))
            msg = payload.get("message", {})
            return "command", (msg.get("argumentText") or msg.get("text") or "").strip(), cmd
        if "messagePayload" in chat:
            msg = chat["messagePayload"].get("message", {})
            slash = msg.get("slashCommand")
            if slash:
                return "command", (msg.get("argumentText") or "").strip(), _norm_cmd_id(slash.get("commandId"))
            return "message", (msg.get("argumentText") or msg.get("text") or "").strip(), None
        return "other", "", None
    # legacy format
    if body.get("type") == "ADDED_TO_SPACE":
        return "added", "", None
    if "message" in body:
        msg = body["message"]
        slash = msg.get("slashCommand")
        text = (msg.get("argumentText") or msg.get("text") or "").strip()
        if slash:
            return "command", text, _norm_cmd_id(slash.get("commandId"))
        return "message", text, None
    return "other", "", None

def chat_reply(body, text):
    """Build the synchronous reply in the format matching the event."""
    if body.get("chat") is not None:
        return json.dumps({"hostAppDataAction": {"chatDataAction": {"createMessageAction": {"message": {"text": text}}}}})
    return json.dumps({"text": text})

def _verify_chat_request(request):
    """Verify the Google-signed bearer token that Chat sends with every event.

    Audience is the project number, issuer chat@system.gserviceaccount.com.
    Without this check anyone who finds the public function URL could pose
    as Chat, query log summaries and burn Claude tokens.
    """
    if not CHAT_AUDIENCE:
        print("WARNING: CHAT_AUDIENCE not set — skipping Chat token verification.")
        return True
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return False
    token = auth[len("Bearer "):]
    req = google_auth_requests.Request()

    # New add-on-style deliveries (UA "Google-gsuiteaddons") are signed with
    # Google's standard OIDC keys and sent by the add-ons service agent;
    # audience+issuer alone would accept ANY Google-minted token with our
    # project number as audience, so the sender email is allowlisted.
    allowed_senders = {
        CHAT_ISSUER,
        f"service-{CHAT_AUDIENCE}@gcp-sa-gsuiteaddons.iam.gserviceaccount.com",
    }
    # Add-on-style deliveries set aud to the endpoint URL, legacy events to
    # the project number — accept both, but only from allowlisted senders.
    allowed_audiences = {CHAT_AUDIENCE, f"https://{request.host}"}
    try:
        # audience=None skips the built-in check; aud is validated manually
        # against the allowed set above.
        claims = id_token.verify_oauth2_token(token, req)
        aud = claims.get("aud", "").rstrip("/")
        if aud not in allowed_audiences:
            print(f"Chat token with unexpected audience: {aud}")
        elif claims.get("email") in allowed_senders and claims.get("email_verified", True):
            return True
        else:
            print(f"Chat token from unexpected sender: {claims.get('email')} (iss {claims.get('iss')})")
    except Exception as e:
        print(f"OIDC-key verification failed, trying legacy Chat certs: {e}")

    # Legacy event format: signed with the Chat system account's x509 certs.
    try:
        claims = id_token.verify_token(
            token,
            req,
            audience=CHAT_AUDIENCE,
            certs_url=CHAT_CERTS_URL,
        )
        return claims.get("iss") == CHAT_ISSUER or claims.get("email") == CHAT_ISSUER
    except Exception as e:
        # Log the (unverified!) claims so the sender allowlist can be
        # extended precisely — diagnostics only, never used for auth.
        try:
            import base64
            payload = token.split(".")[1]
            unverified = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            print(f"Chat token verification failed: {e} | unverified claims: "
                  f"iss={unverified.get('iss')} email={unverified.get('email')} aud={unverified.get('aud')}")
        except Exception:
            print(f"Chat token verification failed: {e}")
        return False

@functions_framework.http
def log_intelligence_webhook(request):
    # Chat vs. Scheduler is decided by the payload (a Chat event carries
    # 'message' or 'type'), not the HTTP method — Cloud Scheduler also POSTs.
    # force=True: Cloud Scheduler posts the body without a JSON content type;
    # without force get_json() returns None and the token check 403s the run.
    body = request.get_json(silent=True, force=True) if request.method == 'POST' else None
    is_chat_event = bool(body) and ('message' in body or 'type' in body or 'chat' in body)

    if is_chat_event and not _verify_chat_request(request):
        return json.dumps({"text": "unauthorized"}), 401
    if not is_chat_event and ANALYZE_TOKEN and (body or {}).get('token') != ANALYZE_TOKEN:
        return "forbidden", 403

    if is_chat_event:
        state = load_state()
        kind, text, command_id = parse_chat_event(body)
        try:
            if kind == "added":
                return chat_reply(body, "Hi! Ich bin LogBot. Frag mich zu den hycaheat.com-Logs, z. B. \"welche AI-Crawler waren heute da?\" (Fenster: letzte 24 h). Commands: /remember, /analyze, /status.")
            if kind == "command":
                if command_id == CMD_REMEMBER:
                    return chat_reply(body, cmd_remember(state, text))
                if command_id == CMD_ANALYZE:
                    return chat_reply(body, cmd_analyze())
                if command_id == CMD_STATUS:
                    return chat_reply(body, cmd_status(state))
                return chat_reply(body, f"Unbekanntes Command (ID {command_id}).")
            if kind == "message" and text:
                return chat_reply(body, answer_chat(state, text))
            return chat_reply(body, "Stell mir eine Frage zu den Logs — oder nutze /remember, /analyze, /status.")
        except Exception as e:
            print(f"Chat handling failed: {e}")
            return chat_reply(body, f"Da ging etwas schief: {e}")

    state = load_state()
    if (body or {}).get("mode") == "weekly":
        # Weekly digest: read-only overview from the archive, always posted.
        weekly_digest(state)
        return "OK", 200

    # Scheduled mode (hourly): per-service check, archive, advance cursor.
    # Posts to the space ONLY on severe problems — routine observations
    # accumulate in the baselines and surface in the weekly digest.
    combined_reports = []
    for service in SERVICES:
        s_state = state["services"].get(service, {"last_timestamp": None, "baseline_summary": "No baseline established yet.", "human_instructions": ""})
        logs, latest_ts = get_logs(service, lookback_hours=24, start_from=s_state["last_timestamp"])
        if not logs: continue
        archive_logs(service, logs)
        processed, stats = preprocess_logs(logs)
        is_noteworthy, report, new_baseline = analyze_with_claude(service, processed, stats, s_state)
        state["services"][service].update({"last_timestamp": latest_ts, "baseline_summary": new_baseline})
        if is_noteworthy:
            combined_reports.append(f"*Service: {service}*\n{report}")

    save_state(state)
    if combined_reports:
        requests.post(CHAT_WEBHOOK_URL, json={"text": "\n\n".join(combined_reports)})
    return "OK", 200

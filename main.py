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

SERVICES = ["hycaheat-website-prod", "hycaheat-configurator-prod", "tco-frontend-prod", "tco-backend-prod"]
LLM_BOTS = ["GPTBot", "ClaudeBot", "Googlebot", "CCBot", "PerplexityBot", "OAI-SearchBot", "Applebot", "Bytespider"]

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
    client = cloud_logging.Client(project=PROJECT_ID)
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
    for entry in logs:
        hr = entry.get("httpRequest", {})
        status, ua, url = hr.get("status"), hr.get("userAgent", ""), hr.get("requestUrl", "")
        stats["status_codes"][status] = stats["status_codes"].get(status, 0) + 1
        is_bot = False
        for bot in LLM_BOTS:
            if bot.lower() in ua.lower():
                stats["bot_hits"][bot] += 1
                is_bot = True; break
        if is_bot or (status and status >= 400):
            processed.append({"t": entry.get("timestamp"), "s": status, "u": url, "ua": ua})
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
    
    TASK:
    1. If user sent a message, answer it using the logs. Be technical and detailed.
    2. Identify NEW anomalies.
    3. Update the 'Baseline Summary' based on current logs and user feedback.
    
    FORMAT:
    --- SECTION_BREAK ---
    NOTEWORTHY: [TRUE/FALSE]
    --- SECTION_BREAK ---
    [Your report/response for Google Chat. Answer user directly if they asked.]
    --- SECTION_BREAK ---
    [Updated Baseline Summary]
    """
    headers = {"x-api-key": CLAUDE_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    data = {"model": "claude-sonnet-5", "max_tokens": 2048, "messages": [{"role": "user", "content": prompt}]}
    response = requests.post("https://api.anthropic.com/v1/messages", headers=headers, json=data)
    res_json = response.json()
    full_text = "".join([b["text"] for b in res_json.get("content", []) if b["type"] == "text"])
    parts = full_text.split("--- SECTION_BREAK ---")
    if len(parts) >= 4:
        return "TRUE" in parts[1].upper(), parts[2].strip(), parts[3].strip()
    return True, full_text, service_state['baseline_summary']

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
            f"INTERESTING LOGS (bots & errors): {json.dumps(processed[:60])}"
        )

    prompt = (
        "You are LogBot, the log-analysis assistant for hycaheat.com "
        "(Cloud Run services behind a GCP load balancer).\n\n"
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
        return "Analyse gestartet — der Bericht kommt in ~2 Minuten in den Space (falls es Auffälligkeiten gibt)."
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
            cmd = str(payload.get("appCommandMetadata", {}).get("appCommandId", ""))
            msg = payload.get("message", {})
            return "command", (msg.get("argumentText") or msg.get("text") or "").strip(), cmd
        if "messagePayload" in chat:
            msg = chat["messagePayload"].get("message", {})
            slash = msg.get("slashCommand")
            if slash:
                return "command", (msg.get("argumentText") or "").strip(), str(slash.get("commandId", ""))
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
            return "command", text, str(slash.get("commandId", ""))
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
    # Scheduled mode: per-service anomaly check, archive, advance cursor.
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

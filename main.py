import os
import json
from datetime import datetime, timedelta, timezone
import requests
from google.cloud import logging as cloud_logging
from google.cloud import storage
import functions_framework

# Configuration
PROJECT_ID = os.getenv("PROJECT_ID", "premium-gear-486210-f2")
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY")
CHAT_WEBHOOK_URL = os.getenv("CHAT_WEBHOOK_URL")
STATE_BUCKET = os.getenv("STATE_BUCKET", f"{PROJECT_ID}-log-intelligence-state")
STATE_FILE = "intelligence_state.json"

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
    
    filter_str = f'resource.type="cloud_run_revision" AND resource.labels.service_name="{service_name}" AND timestamp >= "{start_time.strftime("%Y-%m-%dT%H:%M:%SZ")}"'
    entries = client.list_entries(filter_=filter_str, max_results=500)
    
    logs, latest_ts = [], start_from
    for entry in entries:
        ts = entry.timestamp.isoformat() if entry.timestamp else None
        if ts and (not latest_ts or ts > latest_ts): latest_ts = ts
        logs.append({"timestamp": ts, "httpRequest": entry.http_request if entry.http_request else {}, "resource": entry.resource.labels if entry.resource else {}})
    return logs, latest_ts

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

@functions_framework.http
def log_intelligence_webhook(request):
    state = load_state()
    user_message = None
    if request.method == 'POST':
        body = request.get_json(silent=True)
        if body and 'message' in body:
            user_message = body['message'].get('text')
    
    combined_reports = []
    for service in SERVICES:
        s_state = state["services"].get(service, {"last_timestamp": None, "baseline_summary": "No baseline established yet.", "human_instructions": ""})
        logs, latest_ts = get_logs(service, lookback_hours=24, start_from=s_state["last_timestamp"])
        if not logs and not user_message: continue
        processed, stats = preprocess_logs(logs)
        is_noteworthy, report, new_baseline = analyze_with_claude(service, processed, stats, s_state, user_message)
        state["services"][service].update({"last_timestamp": latest_ts, "baseline_summary": new_baseline})
        if user_message and any(cmd in user_message.lower() for cmd in ["ignore", "remember", "normal"]):
            state["services"][service]["human_instructions"] += f"\n- {user_message}"
        if is_noteworthy:
            combined_reports.append(f"*Service: {service}*\n{report}")
    
    save_state(state)
    if combined_reports:
        final_text = "\n\n".join(combined_reports)
        if request.method == 'POST': return json.dumps({"text": final_text})
        requests.post(CHAT_WEBHOOK_URL, json={"text": final_text})
    return "OK", 200

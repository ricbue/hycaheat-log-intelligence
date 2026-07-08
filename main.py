import os
import json
from datetime import datetime, timedelta, timezone
import requests
from google.cloud import logging

# Configuration from Environment Variables
PROJECT_ID = os.getenv("PROJECT_ID", "premium-gear-486210-f2")
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY")
CHAT_WEBHOOK_URL = os.getenv("CHAT_WEBHOOK_URL")

# Bot signatures to track
LLM_BOTS = ["GPTBot", "ClaudeBot", "Googlebot", "CCBot", "PerplexityBot", "OAI-SearchBot", "Applebot", "Bytespider"]

def get_logs(lookback_hours=6):
    """Fetch logs from GCP Cloud Run services using the official library."""
    client = logging.Client(project=PROJECT_ID)
    
    start_time = (datetime.now(timezone.utc) - timedelta(hours=lookback_hours))
    
    # Filter for Cloud Run logs
    filter_str = (
        f'resource.type="cloud_run_revision" AND '
        f'timestamp >= "{start_time.strftime("%Y-%m-%dT%H:%M:%SZ")}"'
    )
    
    print(f"Fetching logs since {start_time.isoformat()}...")
    
    entries = client.list_entries(filter_=filter_str, max_results=500)
    
    logs = []
    for entry in entries:
        # Convert log entry to a dictionary matching our expected format
        log_data = {
            "timestamp": entry.timestamp.isoformat() if entry.timestamp else None,
            "httpRequest": entry.http_request if entry.http_request else {},
            "resource": entry.resource.labels if entry.resource else {}
        }
        logs.append(log_data)
        
    return logs

def preprocess_logs(logs):
    """Reduce log volume by grouping and filtering."""
    processed = []
    stats = {
        "total_count": len(logs),
        "status_codes": {},
        "bot_hits": {bot: 0 for bot in LLM_BOTS},
        "errors": []
    }
    
    for entry in logs:
        http_request = entry.get("httpRequest", {})
        status = http_request.get("status")
        url = http_request.get("requestUrl")
        ua = http_request.get("userAgent", "")
        
        # Track status codes
        stats["status_codes"][status] = stats["status_codes"].get(status, 0) + 1
        
        # Track bots
        is_bot = False
        for bot in LLM_BOTS:
            if bot.lower() in ua.lower():
                stats["bot_hits"][bot] += 1
                is_bot = True
                break
        
        # Capture errors or anomalies
        if status and status >= 400:
            stats["errors"].append({
                "time": entry.get("timestamp"),
                "status": status,
                "url": url,
                "ua": ua
            })
            
        # Add to raw context if it seems interesting (Bot or Error)
        if is_bot or (status and status >= 400):
            processed.append({
                "t": entry.get("timestamp"),
                "s": status,
                "u": url,
                "ua": ua
            })
            
    return processed, stats

def analyze_with_claude(processed_logs, stats):
    """Send summary and processed logs to Claude for intelligence."""
    prompt = f"""
    Analyze the following website logs for hycaheat.com. 
    
    SUMMARY STATS:
    - Total Requests: {stats['total_count']}
    - Status Distribution: {json.dumps(stats['status_codes'])}
    - LLM Bot Hits: {json.dumps(stats['bot_hits'])}
    
    DETAILED INTERESTING LOGS (Bots & Errors):
    {json.dumps(processed_logs[:100], indent=2)}
    
    TASK:
    1. Identify 'Interesting Bot Behavior': Are specific bots focusing on certain technical topics or case studies?
    2. Identify 'Anomalies': Look for unusual patterns, unexpected 404s, or potential security probes. Compare against typical baseline (mostly status 200, occasional 404 from missing assets).
    3. Action Items: Suggest if any SEO, robots.txt, or technical fixes are needed.
    
    Keep the report concise and suitable for a Google Chat notification. Use emojis for readability.
    """
    
    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key": CLAUDE_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json"
    }
    data = {
        "model": "claude-sonnet-5",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": prompt}]
    }
    
    print("Analyzing with Claude...")
    response = requests.post(url, headers=headers, json=data)
    if response.status_code != 200:
        print(f"Claude API Error: {response.text}")
        return "Error analyzing logs with Claude."
        
    return response.json()["content"][0]["text"]

def send_to_google_chat(text):
    """Post the analysis to Google Chat."""
    payload = {"text": text}
    response = requests.post(CHAT_WEBHOOK_URL, json=payload)
    if response.status_code == 200:
        print("Successfully sent to Google Chat.")
    else:
        print(f"Error sending to Google Chat: {response.text}")

def main():
    logs = get_logs(lookback_hours=24) # Check last 24h for the prototype
    if not logs:
        print("No logs found.")
        return
        
    processed_logs, stats = preprocess_logs(logs)
    report = analyze_with_claude(processed_logs, stats)
    
    print("\n--- CLAUDE ANALYSIS ---\n")
    print(report)
    print("\n-----------------------\n")
    
    send_to_google_chat(report)

if __name__ == "__main__":
    main()

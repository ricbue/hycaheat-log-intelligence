# Log Intelligence for Hyca Heat

This project provides automated monitoring and analysis of Google Cloud Run logs using the Claude API. It detects LLM crawler activity, security anomalies, and general system health, sending reports to Google Chat.

## Features

- **LLM Bot Tracking:** Monitors GPTBot, ClaudeBot, PerplexityBot, etc.
- **Anomaly Detection:** Flags security probes, high error rates, and unusual traffic patterns.
- **Claude Analysis:** Uses Claude 3.5 Sonnet to interpret log data and provide actionable insights.
- **Google Chat Integration:** Sends concise, readable reports to a specified webhook.

## Deployment

Designed to run as a **Cloud Run Job** on a schedule (via Cloud Scheduler).

### Environment Variables

- `PROJECT_ID`: Your GCP Project ID.
- `CLAUDE_API_KEY`: Anthropic API Key.
- `CHAT_WEBHOOK_URL`: Google Chat Webhook URL.

## Local Usage

```bash
pip install -r requirements.txt
python main.py
```

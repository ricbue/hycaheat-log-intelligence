# Log Intelligence — How It Works

An LLM-based monitoring system for Cloud Run request logs: a single Cloud
Function (~500 lines of Python, `main.py`) that checks four production
services hourly, alerts to Google Chat only when human action is required,
posts a weekly trend digest, and doubles as an interactive chat bot you can
ask about the logs. Analysis is done by the Claude API; everything
quantitative (log windows, counters, aggregation) is computed
deterministically in code.

## Components

```
Cloud Run services                    GCP project premium-gear-486210-f2
  hycaheat-website-prod ─┐            (tco-* live in project "hyboid",
  hycaheat-configurator ─┤            read cross-project)
  tco-frontend-prod ─────┤
  tco-backend-prod ──────┘
           │ request logs
           ▼
   Cloud Logging  ◄──────────────── logName filter: run.googleapis.com/requests
           │
           ▼
┌─────────────────────────────────────────────────────────┐
│  Cloud Function "log-intelligence" (gen2, python311)    │
│                                                         │
│  hourly (Cloud Scheduler) ──► per-service check         │
│  weekly (Mon 07:00 Berlin) ─► digest from archive       │
│  Google Chat events ────────► interactive Q&A + cmds    │
└──────────┬──────────────────────────────┬───────────────┘
           │ Claude API                   │
           ▼                              ▼
   claude-sonnet-5              GCS state bucket
   (structured output)            state/   intelligence_state.json
           │                      logs/    raw JSONL archive (90d rotation)
           ▼                      stats/   per-day aggregates (kept forever)
   Google Chat space              digests/ weekly digests (kept forever)
   (webhook for reports, Chat app for dialogue)
```

## The hourly check

Every hour, per service:

1. **Query** Cloud Logging from a persisted cursor (`last_timestamp`), capped
   at the 500 most recent request-log entries. The filter restricts to
   `run.googleapis.com/requests` (container stdout would pollute the data
   with status-less lines) and sorts DESCENDING (ascending + `max_results`
   would silently return the *oldest* entries of the window).
2. **Archive** the raw batch as JSONL to
   `gs://<bucket>/logs/<service>/YYYY/MM/DD/HHMMSS.jsonl`. A bucket
   lifecycle rule deletes archive objects after 90 days. The batch is also
   folded into **per-day aggregates** (`stats/<service>/YYYY-MM-DD.json`:
   total, status histogram, per-bot hits, GEO-file hits) — a few hundred
   bytes per day, kept forever, bucketed by entry timestamp; the cursor
   guarantees batches never overlap, so merging is plain addition.
3. **Preprocess deterministically** into two artifacts:
   - `STATS`: total count, status-code histogram, per-bot hit counters for
     ~15 AI crawlers/fetchers (GPTBot, ClaudeBot, PerplexityBot,
     ChatGPT-User, …).
   - `LOGS`: a *filtered sample* — every bot hit, every GEO-file hit
     (`llms.txt`, `facts.md`), every 4xx/5xx, plus up to 3 example lines per
     status code (so e.g. a 301 spike can be root-caused, not just counted).
     Each line carries timestamp, status, **response size**, URL, and user
     agent. Response size matters: it distinguishes a real file from the SPA
     fallback document a catch-all serves for any probed path.
4. **Analyze with Claude** (`claude-sonnet-5`). The prompt contains the
   mode, the service's baseline summary, the human standing instructions,
   STATS, LOGS — and, crucially, the *data contract* (what LOGS does and
   doesn't contain) plus the alerting policy. Structured output (JSON
   schema) guarantees a parseable `{noteworthy, report, updated_baseline}`;
   on any API hiccup the run fails **closed** (no alert, baseline kept).
5. **Alert or accumulate.** `noteworthy` is defined as "a human must act
   right now": service down, sustained user-facing 5xx, or an attack that
   is *actually succeeding*. The prompt forces a test — name the concrete
   operator action; if there is none, don't alert. Everything else (new
   crawlers, 404s, scanner probes, traffic shifts) is folded into the
   baseline and surfaces in the weekly digest instead.
6. **Persist state**: new cursor + updated baseline per service.

## State design (`intelligence_state.json` in GCS)

Three fields per service, with deliberately different ownership:

| Field | Written by | Purpose |
|---|---|---|
| `last_timestamp` | code | query cursor — windows never overlap |
| `baseline_summary` | the model, every hour | compact *facts* (~1500 chars max): traffic patterns, known noise, open observations. Explicitly no policies, thresholds, or counters — those live in the prompt |
| `human_instructions` | humans only (`/remember`) | standing knowledge the model must respect, e.g. the known-benign list of scanner patterns. **Never rewritten by the model**, survives deploys |

`./deploy.sh --snapshot` copies the live state into the repo so the
instruction list and baselines are versioned in git. Cloud Function
filesystems are ephemeral; GCS is the only source of truth.

## The weekly digest

Mondays 07:00 (Europe/Berlin) a second Scheduler job posts an overview to
the Chat space — unconditionally, in German. It is built from the **full
raw-log archive** of the last 7 days (`collect_weekly_stats`), not the
500-entry live cap: daily traffic counts, status histogram, top error
paths, AI-crawler activity, and GEO-file access (`llms.txt` / `facts.md`)
per user agent, distinguishing genuine AI crawlers from SEO scanners. The
accumulated baselines provide the narrative context. Each digest is also
archived to `gs://<bucket>/digests/YYYY-MM-DD.md` (kept forever — the
long-term record of how traffic and error patterns develop).

## The chat bot

The same function is registered as a Google Chat app ("LogBot"). Mentions
are answered synchronously (Chat allows ~30 s, so one Claude call spans all
services and all per-service I/O runs in a thread pool — sequential
fetching took 27–29 s and kept grazing the deadline; a full per-service
analysis takes ~100 s and is therefore delegated to the Scheduler job via
`/analyze`). The answer prompt layers three time horizons and tells the
model to pick by the question's scope: the raw 24 h sample for "right
now", the per-day aggregates (~3 weeks) for trends like crawler activity,
and the archived weekly digests for narrative context. Chat access is
read-only: it never advances cursors, archives logs, or rewrites
baselines. Commands:
`/remember` (append a standing instruction), `/analyze` (trigger the hourly
job), `/status` (cursors + instruction counts, no LLM call).

## Security

The HTTP endpoint is public (`--allow-unauthenticated`) but hardened in
code: Chat events must carry a valid Google-signed bearer token (both the
legacy Chat format and the new add-on format are verified, with a sender
allowlist), and Scheduler triggers must present a shared secret
(`ANALYZE_TOKEN`). Everything else gets 401/403.

## Design principles (learned the hard way)

The first iteration alerted nearly every hour and produced multi-day
phantom escalations. The root causes were all the same shape: things an
LLM is bad at were left to the LLM. The rules that fixed it:

1. **The LLM is an interpreter, not an instrument.** It narrates whatever
   data it is given — it will not notice that the data itself is broken.
   Windows, deduplication, counters, and trend aggregation are computed in
   code; the model only ever explains numbers it received.
2. **Spell out the data contract.** The model once escalated "STATS says 1
   request but LOGS is empty" as a pipeline defect for days — LOGS was a
   filtered sample by design. Anything surprising about the input format
   must be stated in the prompt.
3. **A status code alone never proves a probe succeeded.** SPA/static
   frontends answer *every* unknown path with the index.html fallback —
   200, or 206 when the scanner sends a Range header. Claiming "attack
   succeeding" requires corroboration (non-HTML content type, response
   size deviating from the fallback document).
4. **Alert = concrete action.** The gate is not "anomalous" but "what
   should the operator do right now?" — everything without an answer goes
   to the weekly digest. Curiosity is not an incident.
5. **Keep policy out of model-maintained state.** Baselines the model
   rewrites each cycle drift into self-invented escalation protocols and
   occurrence counters. Facts go in the baseline; rules live in the prompt;
   durable human knowledge goes in `human_instructions`, which the model
   cannot modify.
6. **Fail closed on a severe-only channel.** If the model response can't be
   parsed, post nothing and keep the old baseline — silence is recoverable,
   alert spam destroys trust in the channel.

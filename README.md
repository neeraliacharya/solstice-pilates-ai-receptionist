# Solstice Pilates — AI Voice Receptionist

A production-grade autonomous voice agent that answers a Pilates studio’s phone, books/reschedules/cancels classes against a live Google Calendar, and maintains a Google Sheets CRM — over a real telephone call, in real time, with no human in the loop.

Built with **Python · FastAPI · asyncio · Vapi (telephony) · Groq · Google Gemini · Google Calendar API · Google Sheets API**.

The hard part of a voice agent is not the LLM call. It is everything around it: keeping a phone line alive while a model thinks, recovering when a model truncates a tool call mid-JSON, switching models the instant one rate-limits without the caller hearing dead air, and never double-booking a class under a flaky connection. This repo is about those parts.

-----

## Table of Contents

1. [Architecture](#architecture)
1. [The Four Voice Realities](#the-four-voice-realities)
1. [The Agent Tool Loop](#the-agent-tool-loop)
1. [Key Design Decisions](#key-design-decisions)
1. [Tools](#tools)
1. [Project Structure](#project-structure)
1. [Quick Start](#quick-start)
1. [Phase 2 — Vapi Voice Setup](#phase-2--vapi-voice-setup)
1. [API Reference](#api-reference)
1. [Configuration](#configuration)
1. [Tradeoffs & Production Roadmap](#tradeoffs--production-roadmap)

-----

## Architecture

```
                          ☎  Caller dials the studio number
                          │
                          ▼
┌──────────────────────────────────────────────────────────────────┐
│  Vapi  (telephony layer)                                          │
│  Speech-to-Text  ·  Text-to-Speech  ·  call lifecycle            │
└───────────────────────────┬──────────────────────────────────────┘
                            │  OpenAI-compatible chat-completion request
                            │  POST /vapi/chat/completions  (per caller turn)
                            ▼
┌──────────────────────────────────────────────────────────────────┐
│  FastAPI  (this service)                                          │
│                                                                  │
│  1. Extract latest user turn from Vapi payload                  │
│  2. Dedup guard (drop Vapi retries within 10 s)                 │
│  3. Fragment buffer (merge split speech within 0.5 s)          │
│  4. Hand to ReceptionistAgent  ──────────────┐                 │
│                                               │                 │
│        ┌──────────────────────────────────────▼─────────────┐  │
│        │  ReceptionistAgent  (stateful, per call_id)         │  │
│        │                                                     │  │
│        │   tool loop:  LLM → tool? → execute → LLM → …       │  │
│        │   ├─ smart tool-set selection (CORE vs FULL)        │  │
│        │   ├─ 4-model fallback chain on rate-limit / failure │  │
│        │   ├─ tool_use_failed recovery (parse + execute)     │  │
│        │   └─ empty-response re-prompt guard                 │  │
│        └──────────────────────┬──────────────────────────────┘  │
│                               │ Calendar / Sheets calls          │
│  5. Stream text back as SSE   │                                  │
│     with silent heartbeats ◄──┘                                  │
│     + spoken fillers at model-switch boundaries                 │
└───────────────────────────┬──────────────────────────────────────┘
                            │
              ┌─────────────┴─────────────┐
              ▼                           ▼
   Google Calendar API v3        Google Sheets API v4
   (source of truth for          (Contacts CRM + CallLog
    classes & bookings,           audit trail)
    atomic seat counts)

         …on call end…
   POST /vapi/webhook  →  finalise CallLog row, clear session
```

**Why the LLM lives behind the HTTP boundary, not in Vapi.** Vapi is configured as a `Custom LLM`. For every caller turn it sends an OpenAI-style request to `/vapi/chat/completions`. The entire tool loop — calendar lookups, booking, model fallback — runs server-side. Vapi only ever sees streamed text. It never sees a tool call, a retry, or a model switch. That keeps the telephony layer dumb and the engineering in one place.

-----

## The Four Voice Realities

A chat agent can take three seconds to think and nobody notices. On a phone call, three seconds of silence sounds like a dropped line. These four realities drove most of the code.

### Reality 1 — The line must never go silent

Vapi holds an SSE connection open per turn and times out in ~10–15 s. But a turn can legitimately take longer: a rate-limit back-off, a slow tool chain (`check_class_availability` → `book_class`), or a reasoning model burning tokens on chain-of-thought.

The fix is a two-tier filler system layered over silent heartbeats:

```
while waiting for the agent's next chunk (checked every 1.5 s):
    if caller has heard nothing yet:
        speak a first-tier filler   ("One moment.", "Let me look into that.")
    elif > 3.5 s since the last spoken word:
        speak a second-tier filler  ("Still working on that.", "Almost there.")
    else:
        send a SILENT heartbeat     (keeps Vapi's HTTP alive, no TTS triggered)
```

Spoken fillers are capped (max 3 per turn) so the agent never babbles. Heartbeats are empty SSE deltas — they reset Vapi’s timeout without producing speech.

### Reality 2 — Models rate-limit mid-call, and the caller can’t wait

Free-tier LLM quotas are shallow. When Qwen3 on Groq hits its TPM ceiling mid-call, retrying is pointless — the caller is on the line *now*. So the agent fails forward through a **4-model fallback chain**, speaking a short filler at each switch so the gap is covered:

```
Qwen3-32B  ──rate-limit──►  Gemini 2.0 Flash  ──rate-limit──►  Llama-3.1-8B  ──►  Llama-3.3-70B
   (primary)                   "One moment please."              "Almost there."     "Bear with me."
```

Two voice-specific escalation rules make this work on a phone line:

- **A rate-limit back-off longer than 5 s is treated as a failure, not a wait.** Waiting 8 s would blow Vapi’s connection timeout, so the agent escalates to the next model instead of sleeping.
- **A quota with `limit: 0` (fully exhausted) escalates immediately** — no point retrying a bucket that is structurally empty.

### Reality 3 — Small models emit broken tool calls

Qwen3-32B, under a tight `max_tokens` budget, frequently truncates a tool call mid-JSON or emits a malformed `<function=…>` / `<tool_call>` tag instead of a structured call. Groq rejects these with `tool_use_failed`. Naively, the turn dies.

Instead the agent **parses the failed generation, reconstructs the intended call, executes it, and injects a synthetic tool-call/tool-result pair** into history so the loop continues as if the model had behaved:

```
tool_use_failed
   │
   ├─ regex-match the malformed call  (3 known broken formats: Llama well-formed,
   │                                    Llama missing-slash, Qwen <tool_call>)
   ├─ repair truncated JSON           (balance braces, fix '}-vs-"} quote bug)
   ├─ execute the tool for real
   └─ inject {assistant: tool_call} + {tool: result}  → loop resumes cleanly
```

If the output is too truncated to even identify the tool name, it falls through to the model-switch chain from Reality 2.

### Reality 4 — A booking must never be a guess

The calendar is the single source of truth for seat counts, and the agent operates under real-world flak: Vapi resends the same turn, speech arrives in fragments (“book the…” / “…six pm reformer”), and a reasoning model may re-confirm a booking it already made.

Layered guards keep state correct:

- **Turn-level dedup** — identical `(call_id, message)` within 10 s is dropped.
- **Fragment buffering** — partial utterances within 0.5 s are merged before the LLM sees them.
- **Pre-flight double-booking check** — `book_class` rejects a slot the caller already holds at that date/time.
- **Identity gate** — no scheduling tool runs until a real name *and* phone are collected; placeholder names, `"Unknown"`, and the studio’s own number are explicitly blocked from being used as caller data.
- **Atomic reschedule with rollback** — cancel-old/book-new; if the new booking fails, the original is restored.
- **`booking_complete` latch** — a stale “yes” after a completed booking is absorbed, not re-executed.

-----

## The Agent Tool Loop

The core loop (`src/agent/core.py`) calls the LLM repeatedly until it stops requesting tools, then streams the final text. What makes it production-grade rather than a vanilla loop:

- **Smart tool-set selection.** On the first turn the agent sends only the CORE tool set; the six scheduling tools (~600–800 tokens of schema) are added only when the message — or recent history — signals booking intent. Once any tool fires, the full set is used for the rest of the turn so `check → book` chaining still works.
- **Voice-trimmed schemas.** In voice mode, `log_call` and `lookup_contact` are stripped — they waste tokens and the model would call them needlessly. Call logging is handled by the end-of-call webhook instead.
- **History pruning.** Voice keeps the last 6 messages, chat the last 10 — never severing an orphaned `tool` result from its `assistant` call.
- **Empty-response re-prompt guard.** Reasoning models sometimes spend the whole token budget thinking and return `content=""`. The loop re-prompts once with an explicit hint, then bails to avoid an infinite empty→hint→empty cycle.
- **Single-flight tool dedup** — identical tool calls within one turn execute once.
- **Result short-circuit (voice only)** — a successful conversational tool result (e.g. a booking confirmation) is streamed straight to TTS, skipping a final LLM round-trip and saving a voice turn. `check_class_availability` is deliberately excluded so the model can chain straight into `book_class`.

-----

## Key Design Decisions

### 1. Custom-LLM integration, not Vapi’s built-in agent

Running the agent server-side behind `/vapi/chat/completions` keeps tool calls, retries, and model fallback invisible to the telephony layer. Vapi stays a dumb STT/TTS pipe; all engineering lives in one testable codebase that also powers the Phase 1 chat UI.

### 2. Fail forward, never wait, on a live call

On a phone call latency is the product. The agent treats a long rate-limit back-off as a failure and switches models rather than sleeping, because a caller will hang up before they wait 8 seconds. Every switch is masked by a spoken filler.

### 3. Recover from broken tool calls instead of dying

Small/cheap models are unreliable tool-callers. Rather than only using a large model (slow, rate-limited), the agent uses a cheap primary and *repairs* its mistakes — parsing malformed/truncated tool calls and executing them. This keeps the common path fast and cheap while staying correct.

### 4. Calendar is the source of truth; Sheets is the CRM

Seat counts live in Calendar extended properties (one place, atomic per event). Sheets holds Contacts + an append-only CallLog audit trail — human-readable for studio staff, zero infra. Calendar reads are cached for 60 s and the cache is flushed on every write so counts are never stale after a booking.

### 5. The identity gate is enforced in the tool layer, not just the prompt

Prompts leak. So `book_class`, `upsert_contact`, and friends independently reject placeholder names, `"Unknown"`, sub-6-digit phones, and the studio’s own number — even if the model is talked into trying. The guard is code, not instruction.

-----

## Tools

|Tool                      |What it does                                                                |
|--------------------------|----------------------------------------------------------------------------|
|`list_upcoming_classes`   |List classes in a date range with availability; optional type/weekday filter|
|`check_class_availability`|Check a specific date/time slot — spots available                           |
|`list_caller_bookings`    |Look up all upcoming bookings for a phone number                            |
|`book_class`              |Create booking on Calendar + upsert contact in Sheets (placeholder-guarded) |
|`reschedule_booking`      |Cancel old + book new — atomic, with rollback on failure                    |
|`cancel_booking`          |Remove a booking from a class                                               |
|`lookup_contact`          |Find caller by phone in Sheets                                              |
|`upsert_contact`          |Create or update a contact row                                              |
|`log_call`                |Append a call-log row (chat mode; voice uses the webhook)                   |
|`escalate_to_human`       |Flag a call for human follow-up + log reason                                |

**Escalation policy.** The agent handles bookings, reschedules, cancellations, availability, pricing, hours, and policy questions. It always escalates billing disputes, refund requests, medical concerns, and “speak to a manager.”

-----

## Project Structure

```
.
├── src/
│   ├── app.py                 # FastAPI app: /ask-anything (chat), /vapi/* (voice),
│   │                          #   SSE generator, filler/heartbeat logic, dedup,
│   │                          #   fragment buffer, end-of-call webhook
│   ├── main.py                # Convenience entry point (uvicorn runner)
│   ├── config.py              # Pydantic settings — reads .env
│   ├── agent/
│   │   ├── core.py            # ReceptionistAgent: tool loop, 4-model fallback,
│   │   │                      #   tool_use_failed recovery, empty-response guard
│   │   ├── prompt.py          # System prompts (voice + chat variants), identity gate
│   │   └── tools.py           # Tool schemas + implementations + dispatch
│   ├── integrations/
│   │   ├── calendar.py        # Google Calendar v3 — classes, bookings, seat counts
│   │   └── sheets.py          # Google Sheets v4 — Contacts CRM + CallLog audit
│   ├── models/
│   │   └── schemas.py         # ClassSlot and related typed models
│   └── utils/
│       ├── cache.py           # timed_cache decorator (Calendar read cache)
│       └── logger.py          # Structured logging setup
├── ui/
│   └── app.py                 # Gradio chat UI (Phase 1 testing)
├── scripts/
│   ├── seed.py                # Seed 8 days of classes with realistic fill levels
│   ├── clear_calendar.py      # Reset calendar test data
│   └── clear_sheets.py        # Reset Sheets test data
├── .env.example
├── requirements.txt
└── README.md
```

-----

## Quick Start

### Prerequisites

- Python 3.11+
- A Google Cloud **service account** with Calendar + Sheets API enabled
- A free [Groq API key](https://console.groq.com)
- (Optional) a [Google Gemini key](https://aistudio.google.com) for the fallback chain
- (Phase 2 only) a [Vapi account](https://dashboard.vapi.ai) for live phone calls

### 1. Install

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

### 2. Add service-account credentials

Place your Google service account JSON at:

```
config/service-account.json
```

Find the `client_email` field inside it — you’ll share your Calendar and Sheet with this address.

### 3. Configure environment

```bash
cp .env.example .env
```

|Variable                     |Where to get it                                                  |
|-----------------------------|-----------------------------------------------------------------|
|`GOOGLE_SERVICE_ACCOUNT_PATH`|Path to the JSON above (default: `./config/service-account.json`)|
|`GOOGLE_CALENDAR_ID`         |Google Calendar → Settings → your calendar → “Calendar ID”       |
|`GOOGLE_SHEET_ID`            |The long ID in the Sheets URL: `.../spreadsheets/d/THIS_ID/edit` |
|`STUDIO_TIMEZONE`            |e.g. `America/Los_Angeles`                                       |
|`GROQ_API_KEY`               |[console.groq.com](https://console.groq.com)                     |
|`GROQ_VOICE_MODEL`           |e.g. `qwen/qwen3-32b` (primary in the fallback chain)            |
|`GROQ_CHAT_MODEL`            |e.g. `llama-3.3-70b-versatile`                                   |
|`GEMINI_API_KEY`             |[aistudio.google.com](https://aistudio.google.com) (fallback)    |
|`VAPI_API_KEY`               |[dashboard.vapi.ai](https://dashboard.vapi.ai) (Phase 2 only)    |

### 4. Share Google resources with the service account

**Calendar:** Settings → your calendar → “Share with specific people” → add the service-account `client_email` → permission **“Make changes to events”**.

**Sheet:** Create a new Sheet → Share → add the `client_email` as **Editor**. The seed script creates the `Contacts` and `CallLog` tabs automatically.

### 5. Seed test data

```bash
python scripts/seed.py
```

Creates 8 days of classes with realistic fill levels. Idempotent — safe to re-run.
Key demo fixtures: **Thursday 6 PM Reformer = FULL, 7 PM = 2 spots left.**

### 6. Run

```bash
# Terminal 1 — API server
uvicorn src.app:app --reload --port 8000

# Terminal 2 — Gradio chat UI (Phase 1 testing)
python ui/app.py
```

Open **<http://localhost:7860>** to chat with the agent before wiring up the phone.

-----

## Phase 2 — Vapi Voice Setup

1. Create an assistant in [dashboard.vapi.ai](https://dashboard.vapi.ai).
1. Set **Model → Provider** to `Custom LLM`.
1. Set **Custom LLM URL** to `https://your-public-url/vapi/chat/completions`.
1. Set **Server URL** (webhook events) to `https://your-public-url/vapi/webhook`.
1. Set **First Message**: `"Thank you for calling Solstice Pilates! How can I help you today?"`
1. Pick a natural voice (ElevenLabs / PlayHT recommended).
1. Buy a phone number → assign it to the assistant.

Use `ngrok http 8000` (or any tunnel) to expose your local server during testing.

-----

## API Reference

### Phase 1 — Chat

|Method|Path           |Body                                      |Response             |
|------|---------------|------------------------------------------|---------------------|
|`POST`|`/ask-anything`|`{ "prompt": "...", "session_id": "..." }`|`{ "answer": "..." }`|

### Phase 2 — Voice (Vapi)

|Method|Path                    |Purpose                                                                        |
|------|------------------------|-------------------------------------------------------------------------------|
|`GET` |`/vapi/chat/completions`|Health probe                                                                   |
|`POST`|`/vapi/chat/completions`|Per-turn Custom-LLM endpoint — runs the agent loop, streams SSE                |
|`GET` |`/vapi/webhook`         |Health probe                                                                   |
|`POST`|`/vapi/webhook`         |Server-side events — finalises CallLog + cleans session on `end-of-call-report`|

`/vapi/chat/completions` speaks the OpenAI streaming SSE dialect (`data: {…}` deltas, terminated by `data: [DONE]`) so Vapi’s Custom-LLM integration consumes it directly.

-----

## Configuration

All configuration is environment-driven via Pydantic settings (`src/config.py`). The fallback-chain models are currently defined in `core.py`; the primary voice/chat models are set in `.env`. Calendar reads are wrapped in a `timed_cache` (default 60 s) and the cache is cleared on every mutation (book/cancel/reschedule) so seat counts are never stale after a write.

-----

## Tradeoffs & Production Roadmap

### Honest tradeoffs made under time pressure

|What                                    |Why it’s acceptable now                                |What changes in production                                                     |
|----------------------------------------|-------------------------------------------------------|-------------------------------------------------------------------------------|
|In-memory session store (`dict`)        |Single-process demo; simple to reason about            |Move sessions to **Redis** so multiple workers / restarts don’t drop live calls|
|Google Sheets as the CRM                |Zero-infra, human-readable, perfect for a single studio|Move Contacts/CallLog to **Postgres**; keep a Sheets export for staff          |
|Fallback-chain models hard-coded in code|Stable during development                              |Make the chain config-driven; add per-model token/latency budgets              |
|Calendar `timed_cache` (60 s)           |Cuts API calls; cleared on every write                 |Use Calendar **push notifications** to invalidate instead of TTL               |
|No auth on `/vapi/*`                    |Internal demo behind an ngrok tunnel                   |**Verify Vapi’s request signature** in middleware                              |
|Concurrency = seat count via Calendar   |Calendar extended-properties hold the booking list     |Under very high concurrency, move seat counts to a row-locked DB to avoid races|
|Prompt is unversioned                   |Stable during development                              |Version prompts; record which version handled each call for auditability       |
|Single Groq client, swapped in place    |Simple model switching                                 |Connection-pool per provider; circuit-breaker on repeated provider failures    |

### Production roadmap (priority order)

1. **Durable sessions** — Redis-backed session + greeting state so a restart mid-call doesn’t lose context.
1. **Request authentication** — verify Vapi signatures on `/vapi/*`; rate-limit per number.
1. **Persistent datastore** — migrate Contacts/CallLog to Postgres; Sheets becomes an export, not the source of truth.
1. **Config-driven model chain** — declare the fallback order, budgets, and thresholds outside the code.
1. **Observability** — structured spans per turn (model used, fallbacks taken, tool latency, fillers spoken) so a call can be replayed from logs.
1. **Race-safe seat counts** — row-locked DB seat reservation for studios running many concurrent lines.
1. **Eval harness** — a suite of scripted call transcripts (book / reschedule / cancel / escalate / out-of-order confirmation) with golden outcomes, run on every prompt or model change.
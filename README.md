# Solstice Pilates — AI Receptionist

This repository contains the backend and agent architecture for a fully autonomous AI Receptionist designed for a boutique Pilates studio. It handles complex scheduling, booking modifications, and caller inquiries over the phone in real-time.

**Phase 1:** Text chat agent &nbsp;|&nbsp; **Phase 2:** Vapi voice agent

## 🛠️ Tech Stack

- **Backend:** Python, FastAPI, Uvicorn, asyncio
- **LLM Inference:** Groq (Qwen/Llama 3), Google Gemini (Fallback)
- **Voice / Telephony:** Vapi (Custom LLM Integration)
- **Database / CRM:** Google Sheets API
- **Scheduling:** Google Calendar API
- **UI (Testing):** Gradio

## 🔄 Architecture & Flow

```text
1. INBOUND CALL
Caller dials in → Vapi handles STT (Speech-to-Text).

2. LLM PROCESSING (SSE Streaming)
Vapi → POST /vapi/llm → FastAPI
FastAPI sends the transcript to the ReceptionistAgent (Groq).
The agent evaluates the conversation history against its strict system prompt (Identity Gate).

3. TOOL EXECUTION
Agent calls necessary tools (e.g., Google Calendar, Google Sheets).
While tools execute or during rate-limit backoffs, FastAPI sends silent HTTP heartbeats to Vapi to keep the connection alive without triggering repetitive TTS fillers.

4. RESPONSE & TTS
Agent streams the final response → FastAPI → Vapi (TTS).

5. POST-CALL WEBHOOK
Vapi → POST /vapi/webhook → FastAPI finalizes the CallLog in Google Sheets.
```

---

## Setup

### 1. Install dependencies

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

### 2. Add your service account credentials

Place your Google service account JSON at:
```
config/service-account.json
```

Find the `client_email` field inside it — you'll share your Google resources with this address.

### 3. Configure environment

```bash
cp .env.example .env
```

Fill in `.env`:

| Variable | Where to get it |
|---|---|
| `GOOGLE_SERVICE_ACCOUNT_PATH` | Path to the JSON above (default: `./config/service-account.json`) |
| `GOOGLE_CALENDAR_ID` | Google Calendar → Settings → your calendar → "Calendar ID" |
| `GOOGLE_SHEET_ID` | The long ID in the Sheets URL: `.../spreadsheets/d/THIS_ID/edit` |
| `GROQ_API_KEY` | [console.groq.com](https://console.groq.com) |
| `VAPI_API_KEY` | [dashboard.vapi.ai](https://dashboard.vapi.ai) (Phase 2 only) |

### 4. Share your Google resources with the service account

**Google Calendar**
1. Open Google Calendar → Settings → your calendar → "Share with specific people"
2. Add the service account `client_email`
3. Permission: **"Make changes to events"**

**Google Sheet**
1. Create a new Google Sheet
2. Share → add the service account `client_email` → **Editor**
3. The seed script will create the `Contacts` and `CallLog` tabs automatically.

### 5. Seed test data

```bash
python scripts/seed.py
```

Creates 8 days of classes with realistic fill levels. Safe to run multiple times — skips events that already exist.

Key fixtures for the demo: **Thursday 6 PM Reformer = FULL, 7 PM = 2 spots left**.

### 6. Run

**Terminal 1 — API server:**
```bash
uvicorn src.app:app --reload --port 8000
```

**Terminal 2 — Chat UI:**
```bash
python ui/app.py
```

Open **http://localhost:7860**

---

## Phase 2 — Vapi voice setup

1. Create an assistant in [dashboard.vapi.ai](https://dashboard.vapi.ai)
2. Set **Model → Provider** to `Custom LLM`
3. Set **Custom LLM URL** to your public endpoint: `https://your-ngrok-url/vapi/llm`
4. Set **Server URL** (for webhook events) to: `https://your-ngrok-url/vapi/webhook`
5. Set **First Message**: `"Thank you for calling Solstice Pilates! How can I help you today?"`
6. Pick a voice (ElevenLabs or PlayHT recommended for naturalness)
7. Buy a phone number → assign to the assistant

---

## Tools

| Tool | What it does |
|---|---|
| `list_upcoming_classes` | List classes in a date range with availability; optional class-type filter |
| `check_class_availability` | Check a specific date/time slot — spots available |
| `list_caller_bookings` | Look up all upcoming bookings for a phone number |
| `book_class` | Create booking on Calendar + save contact in Sheets |
| `reschedule_booking` | Cancel old + book new (atomic with rollback) |
| `cancel_booking` | Remove a booking from a class |
| `lookup_contact` | Find caller by phone in Sheets |
| `upsert_contact` | Create or update a contact row |
| `log_call` | Append call log row (called at end of every conversation) |
| `escalate_to_human` | Flag call for human follow-up + log reason |

## Escalation logic

**Agent handles:** bookings, reschedules, cancellations, availability, pricing, hours, drop-in inquiries, running-late calls, birthday party info.

**Always escalates:** billing disputes, refund requests, medical concerns, "speak to a manager."

---

## Google Sheets schema

**Contacts**
`contact_id | name | phone | email | first_seen | last_seen | total_bookings | notes`

**CallLog**
`call_id | contact_id | name | phone | timestamp | intent | class_name | event_id | outcome | escalated | summary`

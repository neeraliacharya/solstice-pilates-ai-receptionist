import traceback
from contextlib import asynccontextmanager
import time
import json
import asyncio
import random

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from src.agent.core import ReceptionistAgent
from src.integrations.sheets import ensure_headers
from src.utils.logger import get_logger, setup_logging

logger = get_logger(__name__)

MESSAGE_BUFFER: dict[str, list] = {}
MESSAGE_BUFFER_TIMEOUT = 0.5  # seconds — reduced from 1.5 s to cut response latency

async def buffer_and_merge(session_id: str, text: str) -> str | None:
    if session_id not in MESSAGE_BUFFER:
        MESSAGE_BUFFER[session_id] = [text]
        await asyncio.sleep(MESSAGE_BUFFER_TIMEOUT)
        full_message = " ".join(MESSAGE_BUFFER.pop(session_id, []))
        return full_message
    else:
        MESSAGE_BUFFER[session_id].append(text)
        return None  # still accumulating, don't process yet

# ── Startup ────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Re-apply logging config here so it takes effect after uvicorn's own
    # startup sequence (uvicorn calls dictConfig which can reset handlers).
    setup_logging()
    logger.info("=" * 60)
    logger.info("Solstice Pilates AI Receptionist — starting up")
    logger.info("=" * 60)
    try:
        logger.info("Ensuring Google Sheets tabs and headers…")
        ensure_headers()
        logger.info("Google Sheets ready.")
    except Exception as e:
        logger.warning(f"Sheet header check failed (will retry on first use): {e}")
    yield


app = FastAPI(title="Solstice Pilates — AI Receptionist", lifespan=lifespan)

# ── Session store ──────────────────────────────────────────────────────────────
# Keyed by session_id (text chat) or Vapi call_id (voice).
sessions: dict[str, ReceptionistAgent] = {}

# Tracks which voice calls have already received the opening greeting.
# Vapi fires several assistant-request events before the caller speaks; without
# this flag every event that has no user message re-sends the greeting.
greeted_calls: set[str] = set()


def _get_or_create_session(session_id: str, model: str = None, mode: str = "chat") -> ReceptionistAgent:
    if session_id not in sessions:
        agent = ReceptionistAgent(model=model, mode=mode)
        agent.start_conversation()
        sessions[session_id] = agent
        logger.info(f"New session created: {session_id} (mode={mode})")
    return sessions[session_id]


# ── Phase 1: text chat ─────────────────────────────────────────────────────────

class AskRequest(BaseModel):
    prompt: str
    session_id: str


@app.post("/ask-anything")
def ask(request: AskRequest):
    try:
        from src.config import settings
        agent = _get_or_create_session(request.session_id, model=settings.groq_chat_model)
        response = ""
        for chunk in agent.send_stream(request.prompt):
            response = chunk
        return {"answer": response}
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"Error in /ask-anything:\n{tb}")
        err = str(e)
        if "rate_limit_exceeded" in err or "429" in err:
            raise HTTPException(429, "LLM rate limit — please wait a moment and retry.")
        raise HTTPException(500, err)


# ── Phase 2: Vapi voice agent ──────────────────────────────────────────────────
#
# Integration mode: Custom LLM
#   Vapi handles STT + TTS.
#   For each caller turn Vapi sends an OpenAI-compatible chat-completion request
#   to POST /vapi/llm.  We run the full agent tool loop server-side and return
#   only the final text.  Vapi never sees tool calls.
#
# Session lifecycle:
#   Created:  first POST /vapi/llm for a call_id
#   Cleaned:  POST /vapi/webhook with type=end-of-call-report

@app.get("/vapi/chat/completions")
def vapi_llm_probe():
    return {"status": "ok", "note": "POST to this endpoint with an OpenAI chat payload."}


@app.post("/vapi/chat/completions")
async def vapi_llm(request: Request):
    data = await request.json()

    call_id = data.get("call", {}).get("id", "default")
    
    # Extract messages. Vapi uses different fields depending on webhook vs Custom LLM config.
    messages = data.get("messages", [])
    if not messages and "message" in data and isinstance(data["message"], dict):
        # Check for OpenAI formatted array first
        messages = data["message"].get("messagesOpenAIFormatted", [])
        if not messages:
            messages = data["message"].get("messages", [])
            
        if not call_id or call_id == "default":
            call_id = data["message"].get("call", {}).get("id", "default")

    from src.config import settings

    # Check BEFORE creating the session so we know it's genuinely new.
    is_new_call = call_id not in sessions and call_id != "default"

    agent = _get_or_create_session(call_id, model=settings.groq_voice_model, mode="voice")

    # Log the call immediately when it connects — don't wait for end-of-call.
    if is_new_call:
        caller_number = (
            data.get("call", {}).get("customer", {}).get("number")
            or data.get("message", {}).get("call", {}).get("customer", {}).get("number")
            or "Unknown"
        )
        async def _log_inbound():
            try:
                from src.integrations import sheets as sh
                sh.log_call_inbound(call_id, caller_number)
                logger.info(f"[{call_id}] Inbound call logged for {caller_number}")
            except Exception as exc:
                logger.warning(f"[{call_id}] Failed to log inbound call: {exc}")
        asyncio.create_task(_log_inbound())

    # Dedup cache
    if not hasattr(app.state, "recent_requests"):
        app.state.recent_requests = {}

    # Vapi sends the full message history on every request.
    # We only need the latest user turn — our agent tracks its own history.
    user_message = next(
        (m.get("content") or m.get("message", "") for m in reversed(messages) if m.get("role") == "user"),
        "",
    )

    if not user_message:
        # Vapi fires multiple assistant-request events before the caller speaks.
        # We use `greeted_calls` (not Vapi's payload messages) so we send the
        # greeting exactly once per call regardless of how many events arrive.
        if call_id not in greeted_calls:
            greeted_calls.add(call_id)
            greeting = "To get started, could I please get your first and last name, and your mobile number?"
            # Inject into agent history so it knows it already asked for name/phone.
            agent._messages.append({"role": "assistant", "content": greeting})
            logger.info(f"[{call_id}] Sending greeting → {greeting!r}")
            def greeting_stream():
                yield f'data: {{"choices": [{{"delta": {{"content": {json.dumps(greeting)}}}}}]}}\n\n'
                yield 'data: [DONE]\n\n'
            return StreamingResponse(greeting_stream(), media_type="text/event-stream")
        else:
            # Already greeted this call — caller hasn't spoken yet. Return keep-alive.
            logger.debug(f"[{call_id}] No user message and already greeted — returning empty stream.")
            def empty_stream():
                yield 'data: [DONE]\n\n'
            return StreamingResponse(empty_stream(), media_type="text/event-stream")

    now = time.time()
    dedup_key = f"{call_id}:{user_message}"
    last_seen = app.state.recent_requests.get(dedup_key, 0)
    
    if now - last_seen < 10:
        logger.warning(f"[{call_id}] Dropping duplicate request (arrived within 10s): {user_message!r}")
        def empty_stream():
            yield 'data: [DONE]\n\n'
        return StreamingResponse(empty_stream(), media_type="text/event-stream")
        
    app.state.recent_requests[dedup_key] = now
    
    # Buffer fragments
    full_message = await buffer_and_merge(call_id, user_message)
    if not full_message:
        def empty_stream():
            yield 'data: [DONE]\n\n'
        return StreamingResponse(empty_stream(), media_type="text/event-stream")
    
    user_message = full_message
    
    # Cleanup old entries to prevent memory leak
    for k in list(app.state.recent_requests.keys()):
        if now - app.state.recent_requests[k] > 60:
            del app.state.recent_requests[k]

    logger.info(f"[{call_id}] User → {user_message!r}")

    def make_chunk(text: str, finish: bool = False) -> str:
        payload = {
            "choices": [{
                "delta": {"content": text},
                "finish_reason": "stop" if finish else None
            }]
        }
        return f"data: {json.dumps(payload)}\n\n"

    def make_heartbeat() -> str:
        payload = {
            "choices": [{
                "delta": {"content": ""},
                "finish_reason": None
            }]
        }
        return f"data: {json.dumps(payload)}\n\n"

    async def sse_generator():
        # Spoken fillers played while Vapi waits for the agent to respond.
        # First-tier: played when user has heard nothing yet (< 1.5 s silence).
        # Second-tier: played after the first word was spoken but gap > 3.5 s
        #              (prevents "dead air" feeling during long tool chains).
        _FILLERS_FIRST = [
            "One moment. ",
            "Just a second. ",
            "Bear with me. ",
            "Let me look into that. ",
        ]
        _FILLERS_WAIT = [
            "Still working on that. ",
            "Almost there. ",
            "Just a little longer. ",
        ]
        MAX_FILLERS = 3          # cap total spoken fillers from this layer
        SSE_TIMEOUT  = 1.5       # seconds — check queue every 1.5 s
        DEAD_AIR_GAP = 3.5       # seconds of silence before second-tier filler

        try:
            chunk_queue: asyncio.Queue = asyncio.Queue()
            loop = asyncio.get_running_loop()

            def run_agent_sync():
                try:
                    for chunk in agent.send_stream(user_message, is_voice=True):
                        loop.call_soon_threadsafe(chunk_queue.put_nowait, chunk)
                except Exception:
                    logger.exception(f"[{call_id}] Agent thread error")
                finally:
                    loop.call_soon_threadsafe(chunk_queue.put_nowait, None)

            task = asyncio.create_task(asyncio.to_thread(run_agent_sync))

            any_spoken   = False   # True once any spoken content leaves this generator
            last_spoken  = 0.0    # epoch time of last spoken chunk (not heartbeats)
            filler_count = 0      # total spoken fillers yielded by this layer

            while True:
                try:
                    chunk = await asyncio.wait_for(chunk_queue.get(), timeout=SSE_TIMEOUT)
                except asyncio.TimeoutError:
                    now = time.time()
                    if not any_spoken:
                        # User has heard nothing yet — give immediate acknowledgment.
                        if filler_count < MAX_FILLERS:
                            filler_count += 1
                            filler = random.choice(_FILLERS_FIRST)
                            logger.debug(f"[{call_id}] SSE pre-response filler #{filler_count}: {filler!r}")
                            any_spoken  = True
                            last_spoken = now
                            yield make_chunk(filler)
                        else:
                            yield make_heartbeat()
                    elif (now - last_spoken) > DEAD_AIR_GAP and filler_count < MAX_FILLERS:
                        # Something was spoken but it's been > 3.5 s — prevent
                        # "call dropped" feeling during long tool chains.
                        filler_count += 1
                        filler = random.choice(_FILLERS_WAIT)
                        logger.debug(f"[{call_id}] SSE mid-wait filler #{filler_count}: {filler!r}")
                        last_spoken = now
                        yield make_chunk(filler)
                    else:
                        # Still within the gap tolerance — silent heartbeat keeps
                        # Vapi's HTTP connection alive without triggering extra TTS.
                        yield make_heartbeat()
                    continue

                if chunk is None:          # sentinel → thread is done
                    break
                if chunk.strip():          # skip empty / whitespace-only chunks
                    any_spoken  = True
                    last_spoken = time.time()
                    yield make_chunk(chunk)

            await task                     # re-raise any exception from thread
            yield make_chunk("", finish=True)
            yield "data: [DONE]\n\n"       # required by OpenAI SSE spec; Vapi waits for this

        except Exception as e:
            logger.exception("Error in /vapi/llm:")
            yield make_chunk("I'm sorry, I ran into a problem. Let me transfer you to our team.")
            yield make_chunk("", finish=True)
            yield "data: [DONE]\n\n"

    return StreamingResponse(sse_generator(), media_type="text/event-stream")


@app.get("/vapi/webhook")
def vapi_webhook_probe():
    return {"status": "ok", "note": "POST to this endpoint with Vapi server-side events."}


async def log_call_from_report(call_id: str, caller_number: str, transcript: str):
    from src.integrations import sheets as sh

    phone   = caller_number or "Unknown"
    summary = (transcript[:1000] if transcript else "No transcript available")

    # Try to resolve caller name from Contacts sheet (populated during the call).
    try:
        contact = sh.lookup_contact(phone) if phone != "Unknown" else None
        name = contact.get("name", "Unknown") if contact else "Unknown"
    except Exception:
        name = "Unknown"

    logger.info(f"Finalising call log for call_id={call_id}, caller={phone}, name={name}")

    # Update the placeholder row that was written when the call started.
    updated = sh.update_call_log(
        call_id,
        name=name,
        phone=phone,
        outcome="completed",
        intent="completed",
        summary=summary,
    )

    if not updated:
        # Row not found (server restarted mid-call) — append a fresh entry.
        logger.warning(f"[{call_id}] No existing row to update; appending new log entry.")
        sh.log_call(
            name=name,
            phone=phone,
            intent="completed",
            outcome="completed",
            summary=summary,
        )


@app.post("/vapi/webhook")
async def vapi_webhook(request: Request):
    """
    Receives Vapi server-side events.
    Currently used only for session cleanup on call end.
    """
    data = await request.json()
    msg = data.get("message", {})
    msg_type = msg.get("type", "unknown")
    call_id = msg.get("call", {}).get("id", "unknown")

    if msg_type == "end-of-call-report":
        # Extract the specific fields the user wants to track cleanly
        summary = msg.get("analysis", {}).get("summary") or msg.get("summary", "No summary provided")
        transcript = msg.get("transcript", "No transcript provided")
        
        # Find the last bot message in the artifact
        artifact_msgs = msg.get("artifact", {}).get("messages", [])
        last_bot_msg = "None"
        for m in reversed(artifact_msgs):
            if m.get("role") == "bot":
                last_bot_msg = m.get("message", "")
                break
                
        logger.info(f"\n================ VAPI CALL ENDED [{call_id}] ================")
        logger.info(f"Last Bot Message: {last_bot_msg}")
        logger.info(f"Summary: {summary}")
        logger.info(f"Transcript:\n{transcript.strip()}")
        logger.info(f"===============================================================\n")

    if msg_type == "end-of-call-report":
        call_id = msg.get("call", {}).get("id")
        transcript = msg.get("transcript", "")
        caller_number = msg.get("call", {}).get("customer", {}).get("number")
    
        # Log here instead of via agent tool — eliminates duplicate risk entirely
        await log_call_from_report(call_id, caller_number, transcript)
        
        # Clean up session and greeting flag
        sessions.pop(call_id, None)
        greeted_calls.discard(call_id)

    return {"status": "ok"}

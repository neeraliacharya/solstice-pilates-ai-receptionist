import json
import re
import uuid
import threading

from groq import Groq, BadRequestError, RateLimitError

from src.config import settings
from src.agent.prompt import build_system_prompt
from src.agent.tools import TOOL_DECLARATIONS, dispatch
from src.utils.logger import get_logger

logger = get_logger(__name__)


def _build_groq_tools() -> list:
    """Convert TOOL_DECLARATIONS (Gemini-grouped format) to OpenAI/Groq flat list."""
    tools = []
    for group in TOOL_DECLARATIONS:
        for fn in group.get("function_declarations", []):
            tools.append({
                "type": "function",
                "function": {
                    "name": fn["name"],
                    "description": fn.get("description", ""),
                    "parameters": fn.get("parameters", {}),
                },
            })
    return tools


_GROQ_TOOLS = _build_groq_tools()

# Scheduling tools are only sent when the caller's message (or recent history)
# indicates a booking/availability intent.  On turns where the user is just
# giving their name/phone or asking a general question, we send only the core
# tool set — saving ~600-800 input tokens per call.
_SCHEDULING_TOOL_NAMES = frozenset([
    "list_upcoming_classes", "check_class_availability",
    "list_caller_bookings", "book_class", "reschedule_booking", "cancel_booking",
])

# Tools that serve no purpose during a voice call:
#   log_call   — voice prompt explicitly says "do NOT call log_call"; handled by webhook
#   lookup_contact — not needed in voice; causes wasted LLM calls + ~800 extra tokens
_VOICE_EXCLUDED_TOOLS = frozenset(["log_call", "lookup_contact"])

# Chat tool sets (full schema set)
_GROQ_TOOLS_CORE = [t for t in _GROQ_TOOLS if t["function"]["name"] not in _SCHEDULING_TOOL_NAMES]
_GROQ_TOOLS_FULL = _GROQ_TOOLS   # alias for clarity

# Voice tool sets — strip log_call + lookup_contact to cut ~200 schema tokens per request
# and stop the LLM from calling them unnecessarily.
_GROQ_TOOLS_VOICE_CORE = [
    t for t in _GROQ_TOOLS
    if t["function"]["name"] not in (_SCHEDULING_TOOL_NAMES | _VOICE_EXCLUDED_TOOLS)
]
_GROQ_TOOLS_VOICE_FULL = [
    t for t in _GROQ_TOOLS
    if t["function"]["name"] not in _VOICE_EXCLUDED_TOOLS
]

# Keywords that signal the caller wants to check/book/cancel a class.
_SCHEDULING_KEYWORDS = frozenset([
    "book", "schedule", "class", "available", "spot", "check", "cancel",
    "reschedule", "appointment", "session", "slot", "reformer", "mat", "tower",
    "pilates", "friday", "monday", "tuesday", "wednesday", "thursday",
    "saturday", "sunday", "today", "tomorrow", "next week",
    "morning", "evening", "pm", "am", "yes", "yeah", "sure", "go ahead",
    "please", "sounds good", "book it", "that one",
])

# Phrases in recent history that mean we're mid-booking-flow.
_BOOKING_FLOW_PHRASES = (
    "would you like me to book",
    "spots available",
    "currently full",
    "class at",
    "book one of",
    "reformer", "mat pilates", "tower pilates",
)

def _extract_best_json(raw: str) -> str:
    """
    Extract the most complete {...} JSON object from a potentially truncated string.
    Walks the string tracking brace depth; returns the first balanced object.
    If the string is truncated mid-object, returns everything from the first '{'.
    """
    start = raw.find("{")
    if start == -1:
        return raw
    depth = 0
    for i, ch in enumerate(raw[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return raw[start : i + 1]
    # Truncated — return from '{' onwards; _fix_json_quotes will close it
    return raw[start:]


def _fix_json_quotes(raw: str) -> str:
    """
    Fix common JSON corruption emitted by Qwen3:
      • Single-quote before closing braces  e.g.  "2026-05-29'}}  → "2026-05-29"}}
      • Unclosed object (missing one or more closing '}')
    """
    # Replace  '<single-quote><optional-space><}>'  with  '"}'
    fixed = re.sub(r"'(\s*\})", r'"\1', raw)
    # Balance braces if the object was truncated
    opens = fixed.count("{")
    closes = fixed.count("}")
    if opens > closes:
        fixed += "}" * (opens - closes)
    return fixed


class ReceptionistAgent:
    """
    Stateful conversational agent backed by Groq (OpenAI-compatible API).

    Each instance holds a single conversation's message history.
    Create one per session; discard on session end.
    """

    def __init__(self, model: str = None, mode: str = "chat"):
        logger.debug("Initializing ReceptionistAgent")
        self._client = Groq(api_key=settings.groq_api_key, max_retries=0)
        self._model = model or settings.groq_voice_model
        self._mode = mode          # "chat" or "voice" — controls system prompt variant
        self._messages: list[dict] = []
        self._lock = threading.Lock()
        self.booking_complete = False
        self.is_voice = (mode == "voice")

    def start_conversation(self) -> None:
        logger.info(f"Starting new conversation session (mode={self._mode})")
        self._messages = [
            {"role": "system", "content": build_system_prompt(self._mode)}
        ]
        self.booking_complete = False

    from typing import Iterator

    def will_need_tool(self, message: str) -> bool:
        msg_lower = message.lower()
        return any(kw in msg_lower for kw in _SCHEDULING_KEYWORDS)

    def _needs_booking_tools(self, user_message: str) -> bool:
        """
        Return True if scheduling/booking tools should be included in this LLM call.
        Checks both the current user message and the last few history entries so
        short confirmations like "yes please" or "book it" still get the full tool set.
        """
        if any(kw in user_message.lower() for kw in _SCHEDULING_KEYWORDS):
            return True
        # Check recent history for booking-flow context (last 6 entries)
        recent = " ".join(
            (m.get("content") or "") if isinstance(m, dict) else ""
            for m in self._messages[-6:]
        ).lower()
        return any(phrase in recent for phrase in _BOOKING_FLOW_PHRASES)

    def send_stream(self, user_message: str, is_voice: bool = False) -> Iterator[str]:
        """
        Accept a user turn, run the full tool loop, and yield intermediate and final text chunks.
        Handles all tool calls internally — callers receive text chunks as they are generated.
        """
        if self.booking_complete and user_message.strip().lower() in ["yes", "yeah", "yep", "sure", "ok", "okay", "y"]:
            logger.info("Dropping stale confirmation after booking was already completed.")
            yield "Great, you're all set! Have a wonderful day."
            return

        with self._lock:
            if not self._messages:
                self.start_conversation()

            def _get_attr(msg, attr):
                return msg.get(attr) if isinstance(msg, dict) else getattr(msg, attr, None)

            last_user_msg = next((m for m in reversed(self._messages) if _get_attr(m, "role") == "user"), None)
            
            # Deduplicate Vapi retries
            if last_user_msg and _get_attr(last_user_msg, "content") == user_message:
                last_msg = self._messages[-1]
                if _get_attr(last_msg, "role") == "assistant" and _get_attr(last_msg, "content"):
                    logger.warning(f"Duplicate user message detected. Yielding cached response.")
                    yield _get_attr(last_msg, "content")
                    return
                elif _get_attr(last_msg, "role") == "user":
                    logger.warning("Retrying previous user message that did not complete.")
                    # Don't append again, just re-run
                else:
                    self._messages.append({"role": "user", "content": user_message})
            else:
                self._messages.append({"role": "user", "content": user_message})

            yield from self._run_tool_loop_stream(user_message, is_voice=is_voice)

    # ── Internal tool loop ─────────────────────────────────────────────────────

    def _call_llm_with_retries(self, active_tools, max_tokens: int = 400, is_voice: bool = False):
        """
        Generator — yields either filler strings (while waiting to retry) or
        the real ChatCompletion object (exactly once, on success).

        Retries on three failure modes:
          1. Rate-limit (429)      — waits the server-suggested delay with fillers
          1. Rate-limit (429)      — waits the server-suggested delay
          2. Empty-content reply   — Qwen3 reasoning model exhausts max_tokens on
                                     its chain-of-thought, leaving content="".
          3. Transient errors      — 1 s fixed back-off

        BadRequestError is always re-raised immediately so the outer loop's
        tool_use_failed recovery logic can handle it.
        """
        import time
        import random
        max_attempts = 3

        for attempt in range(max_attempts):
            delay = None   # will be set before entering the sleep block

            try:
                response = self._client.chat.completions.create(
                    model=self._model,
                    messages=self._messages,
                    tools=active_tools,
                    tool_choice="auto",
                    max_tokens=max_tokens,
                    temperature=0.3,
                )

                # ── Empty-response detection ──────────────────────────────────
                # Qwen3 spends tokens on internal reasoning first.  When the TPM
                # bucket is nearly empty the model accepts the request but uses
                # all output budget on thinking, leaving content="" + no tool
                # calls.  Retry so the caller always gets a real response.
                msg = response.choices[0].message
                if not msg.tool_calls and not (msg.content or "").strip():
                    if attempt < max_attempts - 1:
                        delay = 1.0
                        logger.warning(
                            f"[{self._model}] Empty response "
                            f"(attempt {attempt + 1}/{max_attempts}) — retrying in {delay}s..."
                        )
                    else:
                        # All retries exhausted — pass through; outer loop handles it
                        yield response
                        return
                else:
                    yield response   # ← real answer ready, pass to Vapi
                    return

            except BadRequestError:
                raise   # tool_use_failed recovery is in the outer loop

            except Exception as e:
                if attempt == max_attempts - 1:
                    logger.error(
                        f"[{self._model}] Model call failed after {max_attempts} attempts: {e}"
                    )
                    raise

                delay = 1.0
                if isinstance(e, RateLimitError):
                    m = re.search(r"try again in ([\d\.]+)s", str(e))
                    if m:
                        delay = float(m.group(1)) + 0.1

                    # Gemini free-tier: quota fully exhausted (limit=0).
                    # Retrying is pointless — escalate immediately to the next model.
                    if re.search(r'"limit":\s*0', str(e)) or "limit: 0" in str(e):
                        logger.warning(
                            f"[{self._model}] Quota exhausted (limit=0) — escalating immediately"
                        )
                        raise

                    # Vapi's SSE response timeout is ~10–15 s.  If the rate-limit
                    # back-off is longer than 5 s, waiting here will cause Vapi to
                    # close the connection before we can retry.  Surface the error
                    # immediately so the outer loop can switch to a faster fallback
                    # model (Gemini → Llama) and respond within Vapi's window.
                    if delay > 5.0:
                        logger.warning(
                            f"[{self._model}] Rate-limit wait {delay:.1f}s exceeds 5 s "
                            f"voice threshold — escalating for model fallback"
                        )
                        raise

                logger.warning(
                    f"[{self._model}] Model call failed "
                    f"(attempt {attempt + 1}/{max_attempts}): {e}. Retrying in {delay:.1f}s..."
                )

            # ── Wait before retry ─────────────────────────────────────────────
            # Sleep silently — the sse_generator sends silent heartbeat SSE chunks
            # during this sleep so Vapi's HTTP connection stays alive without
            # triggering additional TTS output.  Filler phrases are only spoken
            # at model-switch boundaries in the outer loop.
            if delay is not None:
                logger.info(f"[{self._model}] Waiting {delay:.1f}s before retry (silent)...")
                time.sleep(delay)

    def _run_tool_loop_stream(self, user_message: str, is_voice: bool) -> Iterator[str]:
        """
        Call Groq repeatedly until the model stops requesting tool calls.
        Yields filler text during tool execution, and yields the final assistant text.
        """
        last_had_tools = False
        reprompt_done = False   # guard: allow at most 1 re-prompt per user turn

        # Select the appropriate tool set for this mode.
        # Voice tools exclude log_call + lookup_contact to save ~200 schema tokens
        # per request and prevent the LLM from making unnecessary tool calls.
        _tools_core = _GROQ_TOOLS_VOICE_CORE if is_voice else _GROQ_TOOLS_CORE
        _tools_full = _GROQ_TOOLS_VOICE_FULL if is_voice else _GROQ_TOOLS_FULL

        # Decide tool set once per user turn (before any tool calls happen).
        # After the first tool call fires, always use the full set so mid-flow
        # calls (e.g. book_class after check_class_availability) still work.
        first_call_tools = _tools_full if self._needs_booking_tools(user_message) else _tools_core

        while True:
            # --- TOKEN SAVING: Prune history if it gets too long ---
            # Voice: keep last 6 messages (3 turns) — sufficient for booking flow
            # Chat: keep last 10 messages (5 turns)
            history_keep = 6 if is_voice else 10
            if len(self._messages) > (history_keep + 2):
                cutoff = len(self._messages) - history_keep
                # Don't sever an orphaned tool result from its assistant call
                while cutoff > 1 and (self._messages[cutoff].get("role") if isinstance(self._messages[cutoff], dict) else getattr(self._messages[cutoff], "role", None)) == "tool":
                    cutoff -= 1
                self._messages = [self._messages[0]] + self._messages[cutoff:]
            # --------------------------------------------------------

            # First call in this turn: use smart tool set.
            # After a tool has fired (last_had_tools=True): always use full set.
            active_tools = _tools_full if last_had_tools else first_call_tools

            try:
                response = None
                # Voice uses a higher max_tokens budget because Qwen3 is a
                # reasoning model — it burns ~200-350 tokens on chain-of-thought
                # before the actual response. With 400 tokens it can start a tool
                # call and then truncate mid-JSON. 600 gives enough headroom.
                _max_tokens = 600 if is_voice else 400
                for item in self._call_llm_with_retries(active_tools=active_tools, max_tokens=_max_tokens, is_voice=is_voice):
                    if isinstance(item, str):
                        yield item
                    else:
                        response = item
            except RateLimitError as e:
                if "qwen" in self._model:
                    logger.warning(f"Qwen RateLimit — switching to Gemini 2.0 Flash. Details: {e}")
                    gemini_api_key = settings.gemini_api_key
                    if not gemini_api_key:
                        raise  # No Gemini key, let it fail
                    if is_voice:
                        yield "One moment please. "   # caller hears this while Gemini warms up
                    self._client = Groq(
                        api_key=gemini_api_key,
                        base_url="https://generativelanguage.googleapis.com/v1beta/",
                        max_retries=0
                    )
                    self._model = "gemini-2.0-flash"
                    continue  # Retry immediately with Gemini

                elif "gemini" in self._model:
                    logger.warning(f"Gemini RateLimit — switching to Llama-8b. Details: {e}")
                    if is_voice:
                        yield "Almost there. "
                    self._client = Groq(api_key=settings.groq_api_key, max_retries=0)
                    self._model = "llama-3.1-8b-instant"
                    continue  # Retry immediately with Llama-8b

                elif "3.1-8b" in self._model or "llama-3.1-8b" in self._model:
                    # Llama-8b exhausted → try Llama-70b (separate TPM bucket)
                    logger.warning(f"Llama-8b RateLimit — switching to Llama-70b. Details: {e}")
                    if is_voice:
                        yield "Bear with me just a moment. "
                    self._client = Groq(api_key=settings.groq_api_key, max_retries=0)
                    self._model = "llama-3.3-70b-versatile"
                    continue  # Retry with Llama-70b

                else:
                    logger.error(f"{self._model} RateLimit — all fallbacks exhausted. Details: {e}")
                    yield "I'm sorry, our systems are very busy right now. Please try again in a moment."
                    return
            except BadRequestError as exc:
                # Groq rejects the request when the model emits a malformed or
                # truncated tool call (common with Qwen3-32b when max_tokens runs
                # out mid-JSON).  Try to parse and execute the intended tool from
                # the failed_generation.  If that's impossible (output too truncated
                # to identify the tool name), fall through to model-switch so the
                # next fallback model can handle the same turn cleanly.
                body = getattr(exc, "body", {}) or {}
                err_info = body.get("error", {}) if isinstance(body, dict) else {}
                if err_info.get("code") == "tool_use_failed":
                    failed_gen = err_info.get("failed_generation", "")
                    logger.warning(
                        f"[tool_loop] Groq tool_use_failed — recovering from: {failed_gen!r}"
                    )
                    if self._recover_from_failed_generation(failed_gen):
                        last_had_tools = True
                        continue  # re-enter the loop with tool result injected

                    # Recovery impossible (output was too truncated to parse).
                    # Switch to the next model in the fallback chain so the turn
                    # can be retried cleanly — same chain as RateLimitError.
                    logger.warning("[tool_loop] tool_use_failed unrecoverable — attempting model switch")
                    if "qwen" in self._model:
                        gemini_api_key = settings.gemini_api_key
                        if gemini_api_key:
                            logger.warning("Qwen tool_use_failed → switching to Gemini")
                            if is_voice:
                                yield "One moment please. "
                            self._client = Groq(
                                api_key=gemini_api_key,
                                base_url="https://generativelanguage.googleapis.com/v1beta/",
                                max_retries=0,
                            )
                            self._model = "gemini-2.0-flash"
                            continue
                    elif "gemini" in self._model:
                        logger.warning("Gemini tool_use_failed → switching to Llama-8b")
                        if is_voice:
                            yield "Almost there. "
                        self._client = Groq(api_key=settings.groq_api_key, max_retries=0)
                        self._model = "llama-3.1-8b-instant"
                        continue
                    elif "3.1-8b" in self._model:
                        logger.warning("Llama-8b tool_use_failed → switching to Llama-70b")
                        if is_voice:
                            yield "Bear with me. "
                        self._client = Groq(api_key=settings.groq_api_key, max_retries=0)
                        self._model = "llama-3.3-70b-versatile"
                        continue
                    # Llama-70b tool_use_failed — fall through to error below

                # Truly unrecoverable (all models tried, or non-tool_use_failed error)
                logger.error(f"[tool_loop] Unrecoverable BadRequestError: {exc}")
                raise

            msg = response.choices[0].message

            # Store as a plain dict to strip model-specific fields like 'reasoning'
            # (emitted by DeepSeek/QwQ models) that Groq rejects when sent back as
            # input messages in the next turn, causing 400 "reasoning is not supported".
            msg_dict: dict = {"role": msg.role, "content": msg.content}
            if msg.tool_calls:
                msg_dict["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": tc.type,
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ]
            self._messages.append(msg_dict)

            if not msg.tool_calls:
                final_text = msg.content or ""

                # Model returned empty content — re-prompt regardless of whether
                # tools ran this turn.  Covers two cases:
                #   (a) model went silent after tool use (Llama/Qwen bug)
                #   (b) model returned nothing after receiving name/phone — should
                #       have called book_class or asked a follow-up but didn't.
                if not final_text:
                    if reprompt_done:
                        # Already re-prompted once this turn — bail out to avoid an
                        # infinite loop (empty → hint → empty → hint → …).
                        logger.warning("Re-prompt already attempted this turn; yielding empty response.")
                        yield ""
                        return
                    reprompt_done = True
                    # Speak filler immediately so the caller hears something while
                    # the next LLM call (which may switch models on rate-limit) runs.
                    if is_voice:
                        yield "One moment please."
                    hint = (
                        "You returned an empty response. Reply to the caller now.\n"
                        "- If tool results are shown above: summarise them warmly and ask a follow-up.\n"
                        "- If the caller just provided their name and phone number: "
                        "call book_class immediately using the class, date, and time already discussed. "
                        "Do NOT ask for details you already have in the conversation.\n"
                        "- Keep the reply to 1–2 sentences. Do NOT mention tool names or IDs."
                    )
                    logger.info("Empty LLM response — re-prompting with hint")
                    self._messages.append({"role": "user", "content": hint})
                    last_had_tools = True   # guarantee full tool set on next iteration
                    continue               # re-enters while loop → _call_llm_with_retries handles rate-limit fallback

                final_text = self._sanitize_response(final_text)
                logger.info(f"Agent reply: {final_text!r}")
                yield final_text
                return

            # Yield a contextual filler BEFORE executing tools so Vapi TTS
            # starts speaking immediately while the tool is running.
            # Fires on EVERY tool iteration so the caller always hears a
            # verbal acknowledgment for each action (check → book → confirm).
            if is_voice and msg.tool_calls:
                first_tool = msg.tool_calls[0].function.name
                if first_tool == "book_class":
                    yield "Perfect, booking that for you now. "
                elif first_tool == "reschedule_booking":
                    yield "Got it, I'll reschedule that for you. "
                elif first_tool == "cancel_booking":
                    yield "Sure, cancelling that for you now. "
                elif first_tool in ("list_upcoming_classes", "list_caller_bookings"):
                    yield "Let me pull that up for you. "
                else:
                    yield "Let me check that for you. "
            elif not last_had_tools and not is_voice:
                yield "Just a moment while I check the details for you... "

            last_had_tools = True
            tool_results = []
            executed_signatures = set()
            for tc in msg.tool_calls:
                fn_name = tc.function.name
                try:
                    fn_args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    fn_args = {}

                # Deduplicate identical tool calls in the same turn
                sig = (fn_name, json.dumps(fn_args, sort_keys=True))
                if sig in executed_signatures:
                    logger.debug(f"Skipping duplicate tool call in same turn: {fn_name}")
                    tool_results.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "name": fn_name,
                        "content": "Duplicate tool call ignored.",
                    })
                    continue
                executed_signatures.add(sig)

                logger.info(f"Tool call → {fn_name} | args: {fn_args}")
                result = dispatch(fn_name, fn_args)
                logger.info(f"Tool result ← {fn_name}: {result!r}")

                tool_results.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "name": fn_name,
                    "content": str(result),
                })

                # Directly yield conversational tool results to bypass the final LLM generation phase (Voice only).
                # Only shortcut on SUCCESS — errors fall through so the LLM can reformulate them
                # into a natural response instead of reading raw error text aloud via TTS.
                #
                # check_class_availability is intentionally EXCLUDED from this shortcut.
                # Including it caused the loop to return immediately after availability is
                # confirmed, preventing the LLM from chaining straight into book_class on
                # the same turn.  Without the shortcut the LLM sees the availability result
                # and, when the caller already expressed booking intent, calls book_class
                # immediately — one fewer voice round-trip.
                conversational_tools = {"book_class", "list_upcoming_classes", "reschedule_booking", "cancel_booking", "list_caller_bookings"}
                if is_voice and fn_name in conversational_tools and not str(result).startswith("ERROR:"):
                    clean_res = str(result)
                    if fn_name in ["book_class", "reschedule_booking"] and "successfully" in clean_res.lower():
                        self.booking_complete = True
                    yield clean_res
                    self._messages.extend(tool_results)
                    self._messages.append({"role": "assistant", "content": clean_res})
                    return

            self._messages.extend(tool_results)

    def _recover_from_failed_generation(self, failed_gen: str) -> bool:
        """
        Parse a Groq tool_use_failed generation, execute the intended tool, and
        inject a synthetic tool-call / tool-result pair into message history so
        the loop can continue as if the call had succeeded normally.

        Handles three malformed formats:
          <function=name>{"args": ...}</function>   ← Llama well-formed
          <function=name>{"args": ...}<function>    ← Llama malformed (missing '/')
          <tool_call>\n{"name":..., "arguments":{}} ← Qwen3 format
        Returns True if recovery succeeded, False if we couldn't parse it.
        """
        fn_name: str = ""
        raw_args: str = ""

        # ── Format 1: Llama <function=name>...</function|function> ──────────
        llama_pattern = re.compile(
            r'<function=(?P<name>\w+)[>,(]*(?P<args>\{.*?\})[)]?(?:</function>|<function>)',
            re.DOTALL,
        )
        m = llama_pattern.search(failed_gen)
        if m:
            fn_name = m.group("name")
            raw_args = m.group("args")

        # ── Format 2: Qwen3 <tool_call>\n{"name":...,"arguments":{...}} ─────
        if not fn_name:
            qwen_pattern = re.compile(
                r'<tool_call>\s*\{[^}]*"name"\s*:\s*"(?P<name>\w+)"[^}]*"arguments"\s*:\s*(?P<args>\{.*)',
                re.DOTALL,
            )
            m2 = qwen_pattern.search(failed_gen)
            if m2:
                fn_name = m2.group("name")
                raw_args = m2.group("args")
                # The args blob may be truncated — extract the deepest complete {...}
                raw_args = _extract_best_json(raw_args)

        if not fn_name:
            logger.error(f"[recover] Could not parse failed_generation: {failed_gen!r}")
            return False

        # ── Fix common JSON corruption ────────────────────────────────────────
        # Qwen3 sometimes closes string values with ' instead of " before }}
        raw_args = _fix_json_quotes(raw_args)

        try:
            fn_args = json.loads(raw_args)
        except json.JSONDecodeError as exc:
            logger.error(
                f"[recover] JSON parse failed for '{fn_name}': {exc} "
                f"| raw args: {raw_args!r}"
            )
            return False

        logger.info(f"[recover] Executing recovered tool: {fn_name} | args: {fn_args}")
        try:
            result = dispatch(fn_name, fn_args)
            logger.info(f"[recover] Tool result ← {fn_name}: {result!r}")
        except Exception as exc:
            logger.error(f"[recover] Tool execution failed for '{fn_name}': {exc}")
            return False

        # Inject as a proper tool-call / tool-result pair so the message history
        # is structurally identical to a normal tool loop iteration.
        fake_call_id = f"call_{uuid.uuid4().hex[:12]}"
        self._messages.append({
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": fake_call_id,
                "type": "function",
                "function": {
                    "name": fn_name,
                    "arguments": json.dumps(fn_args),
                },
            }],
        })
        self._messages.append({
            "role": "tool",
            "tool_call_id": fake_call_id,
            "name": fn_name,
            "content": str(result),
        })
        logger.info(f"[recover] Injected synthetic tool pair for '{fn_name}' (id: {fake_call_id})")
        return True

    def _sanitize_response(self, text: str) -> str:
        """
        Llama-3 occasionally emits tool calls as inline text tags instead of
        structured tool_calls:
          <function=log_call>{"name":"Alice",...}</function>
        Execute them silently and strip the tag so raw internals never reach the caller.

        Also strips known Llama special tokens (<|eot_id|>, <|python_tag|>, etc.).
        """
        logger.debug(f"[sanitize] Input  ({len(text)} chars): {text!r}")

        # ── 1. Inline function-call tags ─────────────────────────────────────
        # Match both </function> (well-formed) and <function> (Llama's malformed variant)
        fn_pattern = re.compile(
            r'<function=(?P<name>\w+)[>,(]*(?P<args>\{.*?\})[)]?(?:</function>|<function>)',
            re.DOTALL,
        )

        def _execute_and_remove(m: re.Match) -> str:
            fn_name = m.group("name")
            raw_args = m.group("args")
            try:
                fn_args = json.loads(raw_args)
            except json.JSONDecodeError as exc:
                logger.warning(
                    f"[sanitize] Could not parse args for '{fn_name}': {exc} "
                    f"| raw args: {raw_args!r}"
                )
                fn_args = {}
            logger.info(f"[sanitize] Inline tool → {fn_name} | args: {fn_args}")
            try:
                result = dispatch(fn_name, fn_args)
                logger.debug(f"[sanitize] Inline tool result ← {fn_name}: {result!r}")
            except Exception as exc:
                logger.warning(f"[sanitize] Inline tool '{fn_name}' failed: {exc}")
            return ""  # remove the entire tag from the caller-facing text

        cleaned = fn_pattern.sub(_execute_and_remove, text)

        # ── 2. Llama special tokens ───────────────────────────────────────────
        # Only strip well-known token names; avoid nuking legitimate content that
        # happens to sit between <| and |>.
        _LLAMA_TOKENS = re.compile(
            r'<\|(?:python_tag|eot_id|start_header_id|end_header_id|'
            r'begin_of_text|end_of_text|fim_prefix|fim_middle|fim_suffix)\|>'
        )
        cleaned = _LLAMA_TOKENS.sub("", cleaned)
        cleaned = cleaned.strip()

        # ── 3. Log outcome ────────────────────────────────────────────────────
        if cleaned != text.strip():
            stripped_len = len(text.strip()) - len(cleaned)
            logger.info(
                f"[sanitize] Stripped {stripped_len} chars. "
                f"Output ({len(cleaned)} chars): {cleaned!r}"
            )
        else:
            logger.debug("[sanitize] No changes — text passed through unchanged")

        # ── 4. Warn if entire response was consumed by sanitization ───────────
        if not cleaned and text.strip():
            logger.warning(
                "[sanitize] Response was fully stripped (contained only tags). "
                f"Original: {text!r}"
            )

        return cleaned

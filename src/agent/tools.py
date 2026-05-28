import json
from datetime import datetime, timedelta
from typing import Optional
import pytz

from src.config import settings
from src.integrations import calendar as cal
from src.integrations import sheets as sh
from src.utils.logger import get_logger

logger = get_logger(__name__)

# ── Validation constants (module-level so all functions can reference them) ───
_PLACEHOLDERS = {"unknown", "n/a", "", "none", "caller", "john doe", "jane doe", "test", "test user"}
_FAKE_PHONES  = {
    # Studio's own phone — LLM hallucinates this from the system prompt when
    # the caller hasn't provided a real number yet.
    "4155550100", "14155550100",
}

# ── Tool declarations (Gemini function-calling schema) ────────────────────────

TOOL_DECLARATIONS = [
    {
        "function_declarations": [
            {
                "name": "list_upcoming_classes",
                "description": "List classes with availability for next N days. Use when caller names a day of week (pass weekday='Friday'). For exact dates use check_class_availability.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "days_ahead": {"type": ["integer", "string"], "description": "Days ahead. Default 7."},
                        "class_type": {"type": ["string", "null"], "description": "Reformer|Mat|Tower or omit."},
                        "weekday": {"type": ["string", "null"], "description": "e.g. 'Monday'. Omit for all days."},
                    },
                },
            },
            {
                "name": "check_class_availability",
                "description": "Check availability for a specific date (YYYY-MM-DD). For day-of-week queries use list_upcoming_classes.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "date": {"type": "string", "description": "YYYY-MM-DD"},
                        "class_type": {"type": ["string", "null"], "description": "Reformer|Mat|Tower or omit."},
                        "time": {"type": ["string", "null"], "description": "e.g. '06:00 PM'. Omit for all times."},
                    },
                    "required": ["date"],
                },
            },
            {
                "name": "list_caller_bookings",
                "description": "List a caller's upcoming bookings.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "caller_phone": {"type": "string"},
                    },
                    "required": ["caller_phone"],
                },
            },
            {
                "name": "book_class",
                "description": "Book caller into a class. Requires real name and phone (not placeholders).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "date": {"type": "string", "description": "YYYY-MM-DD"},
                        "class_type": {"type": "string", "description": "Reformer|Mat|Tower"},
                        "time": {"type": "string", "description": "e.g. '06:00 PM'"},
                        "caller_name": {"type": "string"},
                        "caller_phone": {"type": "string"},
                    },
                    "required": ["date", "class_type", "time", "caller_name", "caller_phone"],
                },
            },
            {
                "name": "reschedule_booking",
                "description": "Move caller's booking to a new slot. Atomically cancels old and books new.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "caller_phone": {"type": "string"},
                        "caller_name": {"type": "string"},
                        "old_date": {"type": "string", "description": "YYYY-MM-DD"},
                        "old_class_type": {"type": "string"},
                        "old_time": {"type": "string", "description": "e.g. '06:00 PM'"},
                        "new_date": {"type": "string", "description": "YYYY-MM-DD"},
                        "new_class_type": {"type": "string"},
                        "new_time": {"type": "string", "description": "e.g. '06:00 PM'"},
                    },
                    "required": ["caller_phone", "caller_name", "old_date", "old_class_type", "old_time", "new_date", "new_class_type", "new_time"],
                },
            },
            {
                "name": "cancel_booking",
                "description": "Cancel caller's booking.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "caller_phone": {"type": "string"},
                        "date": {"type": "string", "description": "YYYY-MM-DD"},
                        "class_type": {"type": "string"},
                        "time": {"type": "string", "description": "e.g. '06:00 PM'"},
                    },
                    "required": ["caller_phone", "date", "class_type", "time"],
                },
            },
            {
                "name": "lookup_contact",
                "description": "Look up contact by phone number.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "phone": {"type": "string"},
                    },
                    "required": ["phone"],
                },
            },
            {
                "name": "upsert_contact",
                "description": "Save or update caller contact. Call when name+phone collected for first time.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "phone": {"type": "string"},
                        "email": {"type": ["string", "null"]},
                        "notes": {"type": ["string", "null"]},
                    },
                    "required": ["name", "phone"],
                },
            },
            {
                "name": "log_call",
                "description": "Log call outcome. Call at end of conversation (chat mode only).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "caller_name": {"type": "string"},
                        "caller_phone": {"type": "string"},
                        "intent": {"type": "string", "description": "booking|reschedule|cancellation|inquiry|complaint|late_notice|other"},
                        "outcome": {"type": "string", "description": "handled|escalated|info_provided"},
                        "summary": {"type": "string"},
                        "class_name": {"type": ["string", "null"]},
                        "event_id": {"type": ["string", "null"]},
                        "escalated": {"type": "boolean"},
                    },
                    "required": ["caller_name", "caller_phone", "intent", "outcome", "summary"],
                },
            },
            {
                "name": "escalate_to_human",
                "description": "Flag call for human follow-up (billing, refund, medical, manager request).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "caller_name": {"type": ["string", "null"]},
                        "caller_phone": {"type": ["string", "null"]},
                        "reason": {"type": "string", "description": "billing_dispute|refund_request|medical|manager_request|out_of_scope"},
                        "summary": {"type": "string"},
                    },
                    "required": ["reason", "summary"],
                },
            },
        ]
    }
]

# ── Tool implementations ───────────────────────────────────────────────────────


def list_upcoming_classes(
    days_ahead: int | str = 7,
    class_type: Optional[str] = None,
    weekday: Optional[str] = None,
) -> str:
    if class_type and str(class_type).strip().lower() in ["null", "none"]:
        class_type = ""
    if weekday and str(weekday).strip().lower() in ["null", "none"]:
        weekday = ""
    days_ahead = int(days_ahead)
    logger.info(f"Listing upcoming classes for {days_ahead} days (type={class_type}, weekday={weekday})")
    tz = pytz.timezone(settings.studio_timezone)
    today = datetime.now(tz)
    date_from = today.strftime("%Y-%m-%d")
    date_to = (today + timedelta(days=days_ahead)).strftime("%Y-%m-%d")

    slots = cal.list_classes(date_from, date_to)

    # Filter by class type (bidirectional match)
    if class_type:
        q = class_type.lower().strip()
        slots = [s for s in slots if (q in s.class_type.lower()) or (s.class_type.lower() in q)]

    # Filter by day of week
    if weekday:
        target = weekday.strip().lower()
        slots = [s for s in slots if datetime.strptime(s.date, "%Y-%m-%d").strftime("%A").lower() == target]

    if not slots:
        day_part = f" on {weekday.capitalize()}" if weekday else ""
        type_part = f" {class_type}" if class_type else ""
        return f"There are no{type_part} classes scheduled{day_part} in the next {days_ahead} days. They do not exist on the schedule."

    lines = ["Here are the classes I found:"]
    for s in slots:
        status = (
            f"which has {s.available} spot{'s' if s.available != 1 else ''} available"
            if s.available > 0
            else "but it is currently full"
        )
        day_label = datetime.strptime(s.date, "%Y-%m-%d").strftime("%A, %B %-d")
        lines.append(
            f"{s.class_type} on {day_label} at {s.start_time}, {status}."
        )
    lines.append("Would you like me to book one of these for you?")
    return " ".join(lines)


def check_class_availability(
    date: str,
    class_type: Optional[str] = None,
    time: Optional[str] = None,
) -> str:
    if class_type and str(class_type).strip().lower() in ["null", "none"]:
        class_type = ""
    if time and str(time).strip().lower() in ["null", "none"]:
        time = ""

    logger.info(f"Checking availability on {date} (type={class_type}, time={time})")

    # Human-readable date label for TTS ("Friday, May 29")
    try:
        day_label = datetime.strptime(date, "%Y-%m-%d").strftime("%A, %B %-d")
    except ValueError:
        day_label = date  # fallback if date is malformed

    if time:
        # Specific time requested — return the single matching slot.
        slot = cal.find_class(date, class_type, time)
        if not slot:
            desc = f"{class_type} at {time}" if class_type else f"class at {time}"
            return f"The requested {desc} on {day_label} does not exist on the schedule. It is not full, it simply isn't scheduled."
        if slot.available == 0:
            return (
                f"The {slot.class_type} at {slot.start_time} on {day_label} is currently full. "
                "Would you like me to check other times on this day or nearby days?"
            )
        return (
            f"The {slot.class_type} at {slot.start_time} on {day_label} has "
            f"{slot.available} spot{'s' if slot.available != 1 else ''} available. "
            f"Would you like me to book it for you?"
        )

    # No specific time — return ALL matching classes for the day.
    all_slots = cal.list_classes(date, date)
    if class_type:
        q = class_type.lower().strip()
        matching = [s for s in all_slots if (q in s.class_type.lower()) or (s.class_type.lower() in q)]
    else:
        matching = all_slots

    if not matching:
        desc = class_type or "classes"
        return f"There are no {desc} scheduled on {day_label}."

    lines = [f"Here are the classes available on {day_label}:"]
    for s in matching:
        status = (
            f"which has {s.available} spot{'s' if s.available != 1 else ''} available"
            if s.available > 0 else "but it is currently full"
        )
        lines.append(f"{s.class_type} at {s.start_time}, {status}.")
    lines.append("Would you like me to book one of these for you?")
    return " ".join(lines)


def list_caller_bookings(caller_phone: str) -> str:
    logger.info(f"Listing bookings for caller: {caller_phone}")
    bookings = cal.find_caller_bookings(caller_phone)
    if not bookings:
        return "You have no upcoming bookings."
    
    lines = ["Here are your upcoming bookings:"]
    for s in bookings:
        day_label = datetime.strptime(s.date, "%Y-%m-%d").strftime("%A, %B %-d")
        lines.append(f"A {s.class_type} class on {day_label} at {s.start_time}.")
    lines.append("Would you like me to reschedule or cancel any of these?")
    return " ".join(lines)


def book_class(
    date: str,
    class_type: str,
    time: str,
    caller_name: str,
    caller_phone: str,
) -> str:
    # Guard: reject placeholder names/phones that the LLM might hallucinate
    clean_phone = "".join(c for c in caller_phone if c.isdigit())
    if caller_name.strip().lower() in _PLACEHOLDERS:
        return "ERROR: Caller name is required to book a class. Please ask the caller for their real name first."
    if caller_phone.strip().lower() in _PLACEHOLDERS or len(clean_phone) < 6 or clean_phone in _FAKE_PHONES:
        return "ERROR: Caller phone number is required to book a class. Please ask the caller for their real phone number first."

    logger.info(f"Booking class: {class_type} on {date} at {time} for {caller_name}")

    try:
        day_label = datetime.strptime(date, "%Y-%m-%d").strftime("%A, %B %-d")
    except ValueError:
        day_label = date

    # Pre-flight duplicate check: does this caller already have a booking at this date+time?
    existing = cal.find_caller_bookings(caller_phone)
    for booking in existing:
        if booking.date == date and booking.start_time.startswith(time[:5]):
            try:
                dup_label = datetime.strptime(booking.date, "%Y-%m-%d").strftime("%A, %B %-d")
            except ValueError:
                dup_label = booking.date
            return (
                f"This caller already has a {booking.class_type} booking on {dup_label} "
                f"at {booking.start_time}. They cannot be double-booked at the same time."
            )

    slot = cal.find_class(date, class_type, time)
    if not slot:
        return f"No {class_type} class found at {time} on {day_label}."
    if slot.available == 0:
        return "That class is full. Cannot book."

    success, msg = cal.book_class(slot.event_id, caller_name, caller_phone)
    if not success:
        # The calendar layer already returns a friendly duplicate message
        return f"Booking failed: {msg}"

    sh.upsert_contact(caller_name, caller_phone)
    sh.increment_booking_count(caller_phone)

    return (
        f"I have successfully booked you into the {slot.class_type} class at {slot.start_time} on {day_label}. "
        f"Is there anything else I can help you with today?"
    )


def reschedule_booking(
    caller_phone: str,
    caller_name: str,
    old_date: str,
    old_class_type: str,
    old_time: str,
    new_date: str,
    new_class_type: str,
    new_time: str,
) -> str:
    if caller_name.strip().lower() in _PLACEHOLDERS or caller_name == "Unknown":
        return "ERROR: Caller name is missing. Please ask the caller for their real name first."
    if caller_phone.strip().lower() in _PLACEHOLDERS or caller_phone == "Unknown":
        return "ERROR: Caller phone number is missing. Please ask the caller for their real phone number first."

    logger.info(f"Rescheduling {caller_name} from {old_date} {old_time} to {new_date} {new_time}")

    try:
        old_label = datetime.strptime(old_date, "%Y-%m-%d").strftime("%A, %B %-d")
    except ValueError:
        old_label = old_date
    try:
        new_label = datetime.strptime(new_date, "%Y-%m-%d").strftime("%A, %B %-d")
    except ValueError:
        new_label = new_date

    old_slot = cal.find_class(old_date, old_class_type, old_time)
    if not old_slot:
        return f"Could not find original booking: {old_class_type} at {old_time} on {old_label}."

    new_slot = cal.find_class(new_date, new_class_type, new_time)
    if not new_slot:
        return f"New class not found: {new_class_type} at {new_time} on {new_label}."
    if new_slot.available == 0:
        return "New class is full. Cannot reschedule there."

    cancel_ok, cancel_msg = cal.remove_booking(old_slot.event_id, caller_phone)
    if not cancel_ok:
        return f"Could not remove original booking: {cancel_msg}"

    book_ok, book_msg = cal.book_class(new_slot.event_id, caller_name, caller_phone)
    if not book_ok:
        # Rollback
        cal.book_class(old_slot.event_id, caller_name, caller_phone)
        return f"New booking failed, reverted to original class: {book_msg}"

    sh.upsert_contact(caller_name, caller_phone)
    return (
        f"I have successfully rescheduled your class. You are now booked into the {new_class_type} class "
        f"at {new_time} on {new_label}. Is there anything else I can help you with today?"
    )


def cancel_booking(caller_phone: str, date: str, class_type: str, time: str) -> str:
    logger.info(f"Cancelling booking for {caller_phone} on {date} at {time}")
    try:
        day_label = datetime.strptime(date, "%Y-%m-%d").strftime("%A, %B %-d")
    except ValueError:
        day_label = date

    slot = cal.find_class(date, class_type, time)
    if not slot:
        return f"No {class_type} class found at {time} on {day_label}."

    success, msg = cal.remove_booking(slot.event_id, caller_phone)
    if not success:
        return f"Cancellation failed: {msg}"
    return f"I have successfully cancelled your {class_type} class at {time} on {day_label}. Is there anything else I can help you with today?"


def lookup_contact(phone: str) -> str:
    logger.info(f"Looking up contact: {phone}")
    contact = sh.lookup_contact(phone)
    if not contact:
        return "No existing contact found for this phone number."
    return json.dumps(contact)


def upsert_contact(name: str, phone: str, email: Optional[str] = "", notes: Optional[str] = "") -> str:
    if name.strip().lower() in _PLACEHOLDERS or name == "Unknown":
        return "ERROR: Caller name is required. Please ask the caller for their real name."
    if phone.strip().lower() in _PLACEHOLDERS or phone == "Unknown":
        return "ERROR: Caller phone number is required. Please ask the caller for their real phone number."

    logger.info(f"Upserting contact: {name} ({phone})")
    email = email or ""
    notes = notes or ""
    contact = sh.upsert_contact(name, phone, email, notes)
    return f"Contact saved. ID: {contact.get('contact_id', 'unknown')}"


def log_call(
    caller_name: str,
    caller_phone: str,
    intent: str,
    outcome: str,
    summary: str,
    class_name: Optional[str] = "",
    event_id: Optional[str] = "",
    escalated: bool = False,
) -> str:
    logger.info(f"Logging call for {caller_name}: {intent} -> {outcome}")
    class_name = class_name or ""
    event_id = event_id or ""
    contact = sh.lookup_contact(caller_phone)
    contact_id = contact.get("contact_id", "") if contact else ""
    call_id = sh.log_call(
        name=caller_name,
        phone=caller_phone,
        intent=intent,
        outcome=outcome,
        summary=summary,
        contact_id=contact_id,
        class_name=class_name,
        event_id=event_id,
        escalated=escalated,
    )
    return f"Call logged. ID: {call_id}"


def escalate_to_human(
    reason: str,
    summary: str,
    caller_name: Optional[str] = "",
    caller_phone: Optional[str] = "",
) -> str:
    logger.info(f"Escalating to human: {reason} for caller {caller_name}")
    name = caller_name or "Unknown"
    phone = caller_phone or "Unknown"
    if caller_name or caller_phone:
        sh.upsert_contact(name, phone, notes=f"ESCALATION: {reason}")
    sh.log_call(
        name=name,
        phone=phone,
        intent="escalation",
        outcome="escalated",
        summary=f"[ESCALATED — {reason}] {summary}",
        escalated=True,
    )
    return "Escalation logged. A team member will follow up with the caller."


# ── Dispatch ──────────────────────────────────────────────────────────────────

_TOOL_MAP = {
    "list_upcoming_classes": list_upcoming_classes,
    "check_class_availability": check_class_availability,
    "list_caller_bookings": list_caller_bookings,
    "book_class": book_class,
    "reschedule_booking": reschedule_booking,
    "cancel_booking": cancel_booking,
    "lookup_contact": lookup_contact,
    "upsert_contact": upsert_contact,
    "log_call": log_call,
    "escalate_to_human": escalate_to_human,
}


def dispatch(name: str, args: dict) -> str:
    logger.debug(f"Dispatching tool: {name}")
    fn = _TOOL_MAP.get(name)
    if not fn:
        return f"Unknown tool: {name}"
    try:
        return fn(**args)
    except Exception as e:
        return f"Tool error ({name}): {e}"

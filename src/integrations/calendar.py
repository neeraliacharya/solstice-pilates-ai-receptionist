import json
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from datetime import datetime, timedelta
from typing import Optional
import pytz

from src.config import settings
from src.models.schemas import ClassSlot
from src.utils.logger import get_logger
from src.utils.cache import timed_cache, clear_all_caches

logger = get_logger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar"]


def _get_service():
    creds = service_account.Credentials.from_service_account_file(
        settings.google_service_account_path, scopes=SCOPES
    )
    return build("calendar", "v3", credentials=creds)


def _normalize_phone(phone: str) -> str:
    return "".join(c for c in phone if c.isdigit())


def _parse_bookings(props: dict) -> list[dict]:
    """Parse bookings JSON from extended properties. Returns list of {name, phone}."""
    raw = props.get("bookings", "[]")
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []


def _parse_slot(event: dict) -> ClassSlot:
    props = event.get("extendedProperties", {}).get("private", {})
    capacity = int(props.get("capacity", "10"))
    class_type = props.get("class_type", "Class")
    bookings = _parse_bookings(props)
    booked = len(bookings)

    tz = pytz.timezone(settings.studio_timezone)
    start_raw = event["start"].get("dateTime", event["start"].get("date"))
    end_raw = event["end"].get("dateTime", event["end"].get("date"))

    start_dt = datetime.fromisoformat(start_raw.replace("Z", "+00:00")).astimezone(tz)
    end_dt = datetime.fromisoformat(end_raw.replace("Z", "+00:00")).astimezone(tz)

    return ClassSlot(
        event_id=event["id"],
        class_type=class_type,
        date=start_dt.strftime("%Y-%m-%d"),
        start_time=start_dt.strftime("%I:%M %p"),
        end_time=end_dt.strftime("%I:%M %p"),
        capacity=capacity,
        booked=booked,
        available=max(0, capacity - booked),
        title=event.get("summary", ""),
    )


@timed_cache(120)
def list_classes(date_from: str, date_to: str) -> list[ClassSlot]:
    logger.info(f"Fetching calendar events from {date_from} to {date_to}")
    service = _get_service()
    tz = pytz.timezone(settings.studio_timezone)

    start_dt = tz.localize(datetime.strptime(date_from, "%Y-%m-%d"))
    end_dt = tz.localize(datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1))

    result = service.events().list(
        calendarId=settings.google_calendar_id,
        timeMin=start_dt.isoformat(),
        timeMax=end_dt.isoformat(),
        singleEvents=True,
        orderBy="startTime",
    ).execute()

    slots = [_parse_slot(e) for e in result.get("items", [])]

    # Deduplicate: if the calendar has two events at the same class+date+time,
    # keep only the first. Real fix is to delete duplicates in Google Calendar.
    seen: set[tuple] = set()
    unique_slots = []
    for slot in slots:
        key = (slot.class_type, slot.date, slot.start_time)
        if key not in seen:
            seen.add(key)
            unique_slots.append(slot)
        else:
            logger.warning(f"Duplicate calendar event skipped: {slot.class_type} on {slot.date} at {slot.start_time}")

    return unique_slots


@timed_cache(120)
def find_class(
    date: str,
    class_type: Optional[str] = None,
    time: Optional[str] = None,
) -> Optional[ClassSlot]:
    logger.info(f"Finding class on {date} (type: {class_type}, time: {time})")
    slots = list_classes(date, date)
    for slot in slots:
        q, s = (class_type or "").lower().strip(), slot.class_type.lower().strip()
        type_match = class_type is None or (q in s) or (s in q)
        time_match = time is None or slot.start_time.lower() == time.lower()
        if type_match and time_match:
            return slot
    return None


def _update_bookings(service, event_id: str, bookings: list[dict]) -> None:
    """Write the bookings list back to extended properties."""
    service.events().patch(
        calendarId=settings.google_calendar_id,
        eventId=event_id,
        body={
            "extendedProperties": {
                "private": {"bookings": json.dumps(bookings)}
            }
        },
        sendUpdates="none",
    ).execute()
    clear_all_caches()


def book_class(event_id: str, caller_name: str, caller_phone: str) -> tuple[bool, str]:
    logger.info(f"Adding booking for {caller_name} ({caller_phone}) to event {event_id}")
    service = _get_service()
    try:
        event = service.events().get(
            calendarId=settings.google_calendar_id, eventId=event_id
        ).execute()
    except HttpError as e:
        return False, f"Event not found: {e}"

    props = event.get("extendedProperties", {}).get("private", {})
    capacity = int(props.get("capacity", "10"))
    bookings = _parse_bookings(props)

    if len(bookings) >= capacity:
        return False, "Class is full."

    normalized = _normalize_phone(caller_phone)
    if any(_normalize_phone(b["phone"]) == normalized for b in bookings):
        return False, "This number already has a booking in this class."

    bookings.append({"name": caller_name, "phone": caller_phone})
    _update_bookings(service, event_id, bookings)

    return True, "Booked successfully."


def remove_booking(event_id: str, caller_phone: str) -> tuple[bool, str]:
    logger.info(f"Removing booking for {caller_phone} from event {event_id}")
    service = _get_service()
    try:
        event = service.events().get(
            calendarId=settings.google_calendar_id, eventId=event_id
        ).execute()
    except HttpError:
        return False, "Event not found."

    props = event.get("extendedProperties", {}).get("private", {})
    bookings = _parse_bookings(props)
    normalized = _normalize_phone(caller_phone)
    updated = [b for b in bookings if _normalize_phone(b["phone"]) != normalized]

    if len(updated) == len(bookings):
        return False, "No booking found for this number in this class."

    _update_bookings(service, event_id, updated)
    return True, "Booking removed."


@timed_cache(120)
def find_caller_bookings(caller_phone: str, days_ahead: int = 14) -> list[ClassSlot]:
    logger.info(f"Finding existing bookings for caller {caller_phone}")
    tz = pytz.timezone(settings.studio_timezone)
    today = datetime.now(tz)
    date_from = today.strftime("%Y-%m-%d")
    date_to = (today + timedelta(days=days_ahead)).strftime("%Y-%m-%d")

    service = _get_service()
    start_dt = tz.localize(datetime.strptime(date_from, "%Y-%m-%d"))
    end_dt = tz.localize(datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1))

    result = service.events().list(
        calendarId=settings.google_calendar_id,
        timeMin=start_dt.isoformat(),
        timeMax=end_dt.isoformat(),
        singleEvents=True,
        orderBy="startTime",
    ).execute()

    normalized = _normalize_phone(caller_phone)
    booked = []
    for event in result.get("items", []):
        props = event.get("extendedProperties", {}).get("private", {})
        bookings = _parse_bookings(props)
        if any(_normalize_phone(b["phone"]) == normalized for b in bookings):
            booked.append(_parse_slot(event))

    return booked

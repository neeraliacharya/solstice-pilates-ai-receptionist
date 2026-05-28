"""
Seed Google Calendar with Solstice Pilates class schedule and initialize Google Sheets.

Run once before testing:
    .venv/bin/python3 scripts/seed.py

To re-seed: delete the events from Google Calendar first, then run again.
"""
import sys
import os
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timedelta
import pytz
from google.oauth2 import service_account
from googleapiclient.discovery import build

from src.config import settings
from src.integrations.sheets import ensure_headers

TZ = pytz.timezone(settings.studio_timezone)

CAL_SCOPES = ["https://www.googleapis.com/auth/calendar"]

# (class_type, start_hour, start_min, duration_mins, capacity)
WEEKDAY_SCHEDULE = [
    ("Reformer", 6,  0,  55, 8),
    ("Reformer", 7,  0,  55, 8),
    ("Mat",      9,  0,  50, 15),
    ("Tower",    10, 0,  55, 6),
    ("Reformer", 12, 0,  55, 8),
    ("Reformer", 18, 0,  55, 8),
    ("Reformer", 19, 0,  55, 8),
]

WEEKEND_SCHEDULE = [
    ("Mat",      8,  0,  50, 15),
    ("Mat",      9,  30, 50, 15),
    ("Reformer", 11, 0,  55, 8),
]

# Fake members used to pre-fill spots
FAKE_MEMBERS = [
    ("Alex Chen",    "4155550001"),
    ("Jordan Lee",   "4155550002"),
    ("Morgan Davis", "4155550003"),
    ("Taylor Kim",   "4155550004"),
    ("Riley Park",   "4155550005"),
    ("Casey Wang",   "4155550006"),
    ("Avery Patel",  "4155550007"),
    ("Drew Nguyen",  "4155550008"),
    ("Sam Torres",   "4155550009"),
    ("Jamie Okafor", "4155550010"),
    ("Quinn Zhao",   "4155550011"),
    ("Blake Rivera", "4155550012"),
    ("Reese Malik",  "4155550013"),
    ("Finley Chen",  "4155550014"),
    ("Sydney Park",  "4155550015"),
]


def _make_bookings_json(count: int) -> str:
    """Build a JSON bookings string for extended properties (no attendees API needed)."""
    bookings = [
        {"name": name, "phone": phone}
        for name, phone in FAKE_MEMBERS[:count]
    ]
    return json.dumps(bookings)


def _pre_booked_count(is_thursday: bool, hour: int, class_type: str, capacity: int) -> int:
    """Return how many spots to pre-fill for realistic demo data."""
    # Match the assignment example: Thursday 6pm Reformer = FULL, 7pm = 2 spots left
    if is_thursday and hour == 18 and class_type == "Reformer":
        return capacity          # FULL (8/8)
    if is_thursday and hour == 19 and class_type == "Reformer":
        return capacity - 2      # 2 spots left (6/8)
    # Popular times — nearly full
    if hour in (6, 18):
        return max(0, capacity - 3)
    # Midday — half full
    if hour == 12:
        return capacity // 2
    # Morning / evening shoulder — lightly booked
    return max(0, capacity // 4)


def _fetch_existing_keys(service, date_from: str, date_to: str) -> set[tuple]:
    """
    Return a set of (class_type, start_iso) for events already in the calendar,
    so the seed script can skip creating duplicates.
    """
    start_dt = TZ.localize(datetime.strptime(date_from, "%Y-%m-%d"))
    end_dt   = TZ.localize(datetime.strptime(date_to,   "%Y-%m-%d") + timedelta(days=1))

    result = service.events().list(
        calendarId=settings.google_calendar_id,
        timeMin=start_dt.isoformat(),
        timeMax=end_dt.isoformat(),
        singleEvents=True,
    ).execute()

    keys: set[tuple] = set()
    for event in result.get("items", []):
        props = event.get("extendedProperties", {}).get("private", {})
        ct    = props.get("class_type", "")
        start = event["start"].get("dateTime", "")
        if ct and start:
            keys.add((ct, start))
    return keys


def seed_calendar(days_ahead: int = 8) -> None:
    creds = service_account.Credentials.from_service_account_file(
        settings.google_service_account_path, scopes=CAL_SCOPES
    )
    service = build("calendar", "v3", credentials=creds)

    today    = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    date_from = today.strftime("%Y-%m-%d")
    date_to   = (today + timedelta(days=days_ahead - 1)).strftime("%Y-%m-%d")

    print(f"  Checking for existing events ({date_from} → {date_to})…")
    existing = _fetch_existing_keys(service, date_from, date_to)
    print(f"  Found {len(existing)} existing class events — will skip those.\n")

    created = skipped = 0

    for day_offset in range(days_ahead):
        day = today + timedelta(days=day_offset)
        is_weekend  = day.weekday() >= 5
        is_thursday = day.weekday() == 3
        schedule    = WEEKEND_SCHEDULE if is_weekend else WEEKDAY_SCHEDULE

        for class_type, hour, minute, duration, capacity in schedule:
            start  = day.replace(hour=hour, minute=minute, second=0)
            end    = start + timedelta(minutes=duration)
            key    = (class_type, start.isoformat())

            if key in existing:
                print(f"  SKIP  {class_type:10} {start.strftime('%a %m/%d %H:%M')} (already exists)")
                skipped += 1
                continue

            booked = _pre_booked_count(is_thursday, hour, class_type, capacity)
            event  = {
                "summary": f"{class_type} Pilates — Solstice",
                "description": (
                    f"{class_type} Pilates class at Solstice Pilates.\n"
                    f"Max capacity: {capacity} people."
                ),
                "start": {"dateTime": start.isoformat(), "timeZone": settings.studio_timezone},
                "end":   {"dateTime": end.isoformat(),   "timeZone": settings.studio_timezone},
                "extendedProperties": {
                    "private": {
                        "class_type": class_type,
                        "capacity":   str(capacity),
                        "bookings":   _make_bookings_json(booked),
                    }
                },
            }

            result = service.events().insert(
                calendarId=settings.google_calendar_id,
                body=event,
                sendUpdates="none",
            ).execute()

            status = "FULL" if booked == capacity else f"{capacity - booked} spots left"
            print(
                f"  CREATE {class_type:10} {start.strftime('%a %m/%d %H:%M')} "
                f"({booked}/{capacity} — {status}) → {result['id']}"
            )
            created += 1

    print(f"\n✓ Created {created} events, skipped {skipped} existing.")


def seed_sheets() -> None:
    ensure_headers()
    print("✓ Google Sheets headers initialized (Contacts + CallLog tabs).")


if __name__ == "__main__":
    print("=" * 60)
    print("Solstice Pilates — Seed Script")
    print("=" * 60)
    print()
    print("Seeding Google Calendar...")
    seed_calendar()
    print()
    print("Initializing Google Sheets...")
    seed_sheets()
    print()
    print("Done!")
    print()
    print("Next steps:")
    print("  1. Copy .env.example → .env and fill in your values")
    print("  2. Run:  python ui/app.py")
    print("  3. Open: http://localhost:7860")

"""
Delete all Solstice Pilates class events from Google Calendar.

Run this before re-seeding to get a clean slate:
    .venv/bin/python3 scripts/clear_calendar.py
    .venv/bin/python3 scripts/seed.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timedelta
import pytz
from google.oauth2 import service_account
from googleapiclient.discovery import build

from src.config import settings

TZ      = pytz.timezone(settings.studio_timezone)
SCOPES  = ["https://www.googleapis.com/auth/calendar"]


def clear_calendar(days_ahead: int = 30) -> None:
    creds   = service_account.Credentials.from_service_account_file(
        settings.google_service_account_path, scopes=SCOPES
    )
    service = build("calendar", "v3", credentials=creds)

    today    = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    time_min = today.isoformat()
    time_max = (today + timedelta(days=days_ahead)).isoformat()

    print(f"Fetching events from {time_min[:10]} → {time_max[:10]} …")

    page_token = None
    deleted = 0

    while True:
        result = service.events().list(
            calendarId=settings.google_calendar_id,
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            pageToken=page_token,
        ).execute()

        for event in result.get("items", []):
            props = event.get("extendedProperties", {}).get("private", {})
            # Only delete events seeded by this app (they have a "class_type" private property)
            if props.get("class_type"):
                title = event.get("summary", "unknown")
                start = event["start"].get("dateTime", "")[:16]
                service.events().delete(
                    calendarId=settings.google_calendar_id,
                    eventId=event["id"],
                ).execute()
                print(f"  DELETED  {title}  [{start}]")
                deleted += 1

        page_token = result.get("nextPageToken")
        if not page_token:
            break

    print(f"\nDone. Deleted {deleted} class events.")


if __name__ == "__main__":
    clear_calendar()

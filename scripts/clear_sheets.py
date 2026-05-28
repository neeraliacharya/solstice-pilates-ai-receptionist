"""
Clear all data rows from the Contacts and CallLog sheets, keeping headers intact.

Run this to reset sheet data before a clean demo:
    .venv/bin/python3 scripts/clear_sheets.py

To also reset the calendar:
    .venv/bin/python3 scripts/clear_calendar.py
    .venv/bin/python3 scripts/seed.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from google.oauth2 import service_account
from googleapiclient.discovery import build

from src.config import settings
from src.integrations.sheets import CONTACTS_SHEET, CALL_LOG_SHEET

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def clear_sheets() -> None:
    creds = service_account.Credentials.from_service_account_file(
        settings.google_service_account_path, scopes=SCOPES
    )
    service = build("sheets", "v4", credentials=creds)

    for sheet_name in [CONTACTS_SHEET, CALL_LOG_SHEET]:
        # Check how many rows currently exist
        result = service.spreadsheets().values().get(
            spreadsheetId=settings.google_sheet_id,
            range=f"{sheet_name}!A1:Z",
        ).execute()
        values = result.get("values", [])
        data_rows = len(values) - 1  # subtract header row

        if data_rows <= 0:
            print(f"  {sheet_name}: already empty, nothing to clear.")
            continue

        # Clear everything from row 2 onwards (leaves header row intact)
        service.spreadsheets().values().clear(
            spreadsheetId=settings.google_sheet_id,
            range=f"{sheet_name}!A2:Z",
        ).execute()
        print(f"  {sheet_name}: cleared {data_rows} data row{'s' if data_rows != 1 else ''}.")

    print("\nDone. Headers preserved — sheets are ready for a clean run.")


if __name__ == "__main__":
    clear_sheets()

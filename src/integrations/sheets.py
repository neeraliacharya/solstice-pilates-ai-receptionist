from google.oauth2 import service_account
from googleapiclient.discovery import build
from datetime import datetime, timezone
from typing import Optional
import uuid

from src.config import settings
from src.utils.logger import get_logger
from src.utils.cache import timed_cache, clear_all_caches

logger = get_logger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

CONTACTS_SHEET = "Contacts"
CALL_LOG_SHEET = "CallLog"

CONTACTS_HEADERS = [
    "contact_id", "name", "phone", "email",
    "first_seen", "last_seen", "total_bookings", "notes",
]
CALL_LOG_HEADERS = [
    "call_id", "contact_id", "name", "phone", "timestamp",
    "intent", "class_name", "event_id", "outcome", "escalated", "summary",
]


def _get_service():
    creds = service_account.Credentials.from_service_account_file(
        settings.google_service_account_path, scopes=SCOPES
    )
    return build("sheets", "v4", credentials=creds)


def _normalize_phone(phone: str) -> str:
    return "".join(c for c in phone if c.isdigit())


def _get_rows(service, sheet_name: str) -> tuple[list[str], list[list[str]]]:
    result = service.spreadsheets().values().get(
        spreadsheetId=settings.google_sheet_id,
        range=f"{sheet_name}!A1:Z",
    ).execute()
    values = result.get("values", [])
    if not values:
        return [], []
    headers = values[0]
    data_rows = [row + [""] * (len(headers) - len(row)) for row in values[1:]]
    return headers, data_rows


def _row_to_dict(headers: list[str], row: list[str]) -> dict:
    return dict(zip(headers, row))


def _dict_to_row(headers: list[str], data: dict) -> list[str]:
    return [str(data.get(h, "")) for h in headers]


def ensure_headers():
    logger.info("Ensuring Sheets headers exist")
    service = _get_service()

    spreadsheet = service.spreadsheets().get(
        spreadsheetId=settings.google_sheet_id
    ).execute()
    existing = {s["properties"]["title"] for s in spreadsheet.get("sheets", [])}

    requests = []
    for sheet_name in [CONTACTS_SHEET, CALL_LOG_SHEET]:
        if sheet_name not in existing:
            requests.append({"addSheet": {"properties": {"title": sheet_name}}})

    if requests:
        service.spreadsheets().batchUpdate(
            spreadsheetId=settings.google_sheet_id,
            body={"requests": requests},
        ).execute()

    for sheet_name, headers in [
        (CONTACTS_SHEET, CONTACTS_HEADERS),
        (CALL_LOG_SHEET, CALL_LOG_HEADERS),
    ]:
        result = service.spreadsheets().values().get(
            spreadsheetId=settings.google_sheet_id,
            range=f"{sheet_name}!A1:A1",
        ).execute()
        if not result.get("values"):
            service.spreadsheets().values().update(
                spreadsheetId=settings.google_sheet_id,
                range=f"{sheet_name}!A1",
                valueInputOption="RAW",
                body={"values": [headers]},
            ).execute()
            print(f"Headers written to {sheet_name}")


@timed_cache(120)
def lookup_contact(phone: str) -> Optional[dict]:
    logger.info(f"Looking up contact by phone: {phone}")
    service = _get_service()
    headers, rows = _get_rows(service, CONTACTS_SHEET)
    if not headers:
        return None
    normalized = _normalize_phone(phone)
    for row in rows:
        d = _row_to_dict(headers, row)
        if _normalize_phone(d.get("phone", "")) == normalized:
            return d
    return None


def upsert_contact(
    name: str,
    phone: str,
    email: str = "",
    notes: str = "",
) -> dict:
    logger.info(f"Upserting contact sheet record for: {phone}")
    service = _get_service()
    headers, rows = _get_rows(service, CONTACTS_SHEET)
    now = datetime.now(timezone.utc).isoformat()
    normalized = _normalize_phone(phone)

    for i, row in enumerate(rows):
        d = _row_to_dict(headers, row)
        if _normalize_phone(d.get("phone", "")) == normalized:
            d["last_seen"] = now
            if name and name.strip():
                d["name"] = name
            if notes:
                existing = d.get("notes", "")
                d["notes"] = f"{existing}; {notes}".strip("; ") if existing else notes
            updated_row = _dict_to_row(headers, d)
            row_number = i + 2  # 1-indexed + header
            service.spreadsheets().values().update(
                spreadsheetId=settings.google_sheet_id,
                range=f"{CONTACTS_SHEET}!A{row_number}",
                valueInputOption="RAW",
                body={"values": [updated_row]},
            ).execute()
            clear_all_caches()
            return d

    contact_id = str(uuid.uuid4())[:8]
    new_contact: dict = {
        "contact_id": contact_id,
        "name": name,
        "phone": phone,
        "email": email,
        "first_seen": now,
        "last_seen": now,
        "total_bookings": "0",
        "notes": notes,
    }
    service.spreadsheets().values().append(
        spreadsheetId=settings.google_sheet_id,
        range=f"{CONTACTS_SHEET}!A1",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": [_dict_to_row(CONTACTS_HEADERS, new_contact)]},
    ).execute()
    clear_all_caches()
    return new_contact


def increment_booking_count(phone: str) -> None:
    logger.info(f"Incrementing booking count for: {phone}")
    service = _get_service()
    headers, rows = _get_rows(service, CONTACTS_SHEET)
    normalized = _normalize_phone(phone)
    for i, row in enumerate(rows):
        d = _row_to_dict(headers, row)
        if _normalize_phone(d.get("phone", "")) == normalized:
            current = int(d.get("total_bookings", "0") or "0")
            d["total_bookings"] = str(current + 1)
            updated_row = _dict_to_row(headers, d)
            row_number = i + 2
            service.spreadsheets().values().update(
                spreadsheetId=settings.google_sheet_id,
                range=f"{CONTACTS_SHEET}!A{row_number}",
                valueInputOption="RAW",
                body={"values": [updated_row]},
            ).execute()
            break


def log_call(
    name: str,
    phone: str,
    intent: str,
    outcome: str,
    summary: str,
    contact_id: str = "",
    class_name: str = "",
    event_id: str = "",
    escalated: bool = False,
) -> str:
    logger.info(f"Appending call log for {phone}: {intent}")
    service = _get_service()
    now = datetime.now(timezone.utc).isoformat()
    call_id = str(uuid.uuid4())[:8]
    row_data = {
        "call_id": call_id,
        "contact_id": contact_id,
        "name": name,
        "phone": phone,
        "timestamp": now,
        "intent": intent,
        "class_name": class_name,
        "event_id": event_id,
        "outcome": outcome,
        "escalated": str(escalated),
        "summary": summary,
    }
    service.spreadsheets().values().append(
        spreadsheetId=settings.google_sheet_id,
        range=f"{CALL_LOG_SHEET}!A1",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": [_dict_to_row(CALL_LOG_HEADERS, row_data)]},
    ).execute()
    return call_id


def log_call_inbound(vapi_call_id: str, caller_number: str) -> None:
    """
    Write a placeholder row to CallLog the moment a voice call connects.
    Uses the Vapi call_id as the row key so it can be updated at call end.
    """
    logger.info(f"Logging inbound call: vapi_call_id={vapi_call_id}, caller={caller_number}")
    service = _get_service()
    now = datetime.now(timezone.utc).isoformat()
    row_data = {
        "call_id":    vapi_call_id,          # Vapi UUID — used to find & update this row later
        "contact_id": "",
        "name":       "Unknown",
        "phone":      caller_number or "Unknown",
        "timestamp":  now,
        "intent":     "inbound",
        "class_name": "",
        "event_id":   "",
        "outcome":    "in_progress",
        "escalated":  "False",
        "summary":    "Call in progress…",
    }
    service.spreadsheets().values().append(
        spreadsheetId=settings.google_sheet_id,
        range=f"{CALL_LOG_SHEET}!A1",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": [_dict_to_row(CALL_LOG_HEADERS, row_data)]},
    ).execute()
    clear_all_caches()


def update_call_log(vapi_call_id: str, **fields) -> bool:
    """
    Find the CallLog row whose call_id equals *vapi_call_id* and patch it
    with the provided keyword fields.  Returns True on success, False if the
    row was not found (e.g. server restarted between call start and end).
    """
    logger.info(f"Updating call log for vapi_call_id={vapi_call_id}: {list(fields.keys())}")
    service = _get_service()
    headers, rows = _get_rows(service, CALL_LOG_SHEET)
    if not headers or "call_id" not in headers:
        logger.warning("update_call_log: CallLog sheet has no headers yet.")
        return False

    id_col = headers.index("call_id")

    for i, row in enumerate(rows):
        if len(row) > id_col and row[id_col] == vapi_call_id:
            d = _row_to_dict(headers, row)
            d.update(fields)
            updated_row = _dict_to_row(headers, d)
            row_number = i + 2          # sheet rows are 1-indexed, +1 for header
            service.spreadsheets().values().update(
                spreadsheetId=settings.google_sheet_id,
                range=f"{CALL_LOG_SHEET}!A{row_number}",
                valueInputOption="RAW",
                body={"values": [updated_row]},
            ).execute()
            clear_all_caches()
            logger.info(f"Call log row {row_number} updated for {vapi_call_id}.")
            return True

    logger.warning(f"update_call_log: no row found for vapi_call_id={vapi_call_id}.")
    return False

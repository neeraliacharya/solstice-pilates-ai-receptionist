from pydantic import BaseModel
from typing import Optional


class ClassSlot(BaseModel):
    event_id: str
    class_type: str
    date: str        # YYYY-MM-DD
    start_time: str  # 12-hour with AM/PM, e.g. "06:00 PM"
    end_time: str    # same format
    capacity: int
    booked: int
    available: int
    title: str


class Contact(BaseModel):
    contact_id: str
    name: str
    phone: str
    email: Optional[str] = ""
    first_seen: str
    last_seen: str
    total_bookings: int = 0
    notes: Optional[str] = ""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, field_validator, Field

# To add: example values if time allows


class Slot(BaseModel):
    id: str
    provider: str
    start: datetime
    end: datetime

class SlotWithAvailability(Slot):
    available: bool


class ResetRequest(BaseModel):
    slots: list[Slot]


class SlotsResponse(BaseModel):
    slots: list[SlotWithAvailability]


class NewBooking(BaseModel):
    # Optional on purpose: the spec wants a 400 (not FastAPI's 422)
    # for missing fields, so we validate these ourselves at level 2.
    slot_id: str | None = None
    name: str | None = None
    email: str | None = None


class Booking(BaseModel):
    id: str
    slot_id: str
    name: str
    email: str
    status: Literal["confirmed", "cancelled"]
    created_at: datetime


class BookingsPage(BaseModel):
    bookings: list[Booking]
    next_cursor: str | None = None


class Error(BaseModel):
    error: str

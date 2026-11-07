import asyncio
import base64
import json
import re
import uuid
from datetime import datetime, timezone

from fastapi import status
from fastapi.exceptions import HTTPException
from fastapi.responses import JSONResponse
from postgrest.exceptions import APIError

from database import supabase
from schema import Booking, NewBooking


# Function for lvl 1 - 3
def create_booking(new_booking: NewBooking) -> Booking:
    """Creates a booking without an idempotence key"""
    # Validation 1: check for missing or empty string fields
    is_missing_or_empty = (
        not hasattr(new_booking, "slot_id")
        or not hasattr(new_booking, "name")
        or not hasattr(new_booking, "email")
        or not str(new_booking.slot_id or "").strip()
        or not str(new_booking.name or "").strip()
        or not str(new_booking.email or "").strip()
    )
    if is_missing_or_empty:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="slot_id or name or email is missing or empty",
        )

    # Validation 2: check for invalid fields (all fields are valid strings atp)
    # Email regex pattern: exactly one '@', something before it,
    # at least a dot in the domain part (non-empty parts on both
    # sides of every dot), and no spaces
    EMAIL_REGEX = r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$"

    # Trim spaces from name and email
    new_booking.name = new_booking.name.strip()
    # Store email as lowercase too
    new_booking.email = new_booking.email.strip().lower()

    # Validate email (Must match regex)
    email_is_invalid = not re.match(EMAIL_REGEX, new_booking.email)
    if email_is_invalid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="email is invalid"
        )

    # Checks if a slot doesn't exist
    slot_exists = (
        supabase.table("slots").select("*").eq("id", new_booking.slot_id).execute().data
    )
    if slot_exists == []:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="slot doesn't exist"
        )

    # Check if a slot has already started
    time_now = datetime.now(timezone.utc)  # noqa: UP017
    start_time = datetime.fromisoformat(slot_exists[0]["start_time"])

    if time_now >= start_time:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="slot has already started"
        )

    # A partial unique index in db on confirmed slots.
    # Checks if a slot has a confirmed booking. If so,
    # 409. Otherwise, its added to the bookings table
    try:
        new_booking_row = new_booking.model_dump(mode="json")
        new_booking_row["status"] = "confirmed"
        booking_row = (
            supabase.table("bookings").insert(new_booking_row).execute().data[0]
        )
    except APIError as e:
        print(f"PostgREST Error Code: {e.code}")
        print(f"Message: {e.message}")
        if e.code == "23505":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="slot already has a confirmed booking",
            )
        else:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

    # Return Booking instance
    booking = Booking.model_validate(booking_row)
    return booking


# Helper functions for lvl 4 - Optional idempotence key
def clean_body(b: NewBooking) -> dict:
    """Clean request body"""
    return {
        "slot_id": b.slot_id.strip() if b.slot_id else b.slot_id,
        "name": b.name.strip() if b.name else b.name,
        "email": b.email.strip().lower() if b.email else b.email,
    }


def save_result(key: str, status_code: int, body: dict):
    """Save status code and response with the key"""
    supabase.table("idempotency_keys").update(
        {"status_code": status_code, "response_body": body}
    ).eq("key", key).execute()


def release_key(key: str):
    """For concurrent requests: Delete key when one request
    fails to save a response as the other was waiting"""
    supabase.table("idempotency_keys").delete().eq("key", key).execute()


async def replay(key: str, body: dict):
    """For concurrent requests: A request retries multiple
    times to get the results for a key until it appears"""
    for _ in range(20):  # waits up to about 2 seconds
        # Figure out which body took the key
        rows = (
            supabase.table("idempotency_keys").select("*").eq("key", key).execute().data
        )
        if not rows:
            raise HTTPException(status_code=500, detail="Please retry the request")
        # Determine a response based on which body it was
        row = rows[0]
        if row["request_body"] != body:
            raise HTTPException(
                status_code=422, detail="Idempotency-Key reused with a different body"
            )
        if row["status_code"] is not None:
            return JSONResponse(
                status_code=row["status_code"], content=row["response_body"]
            )
        await asyncio.sleep(0.1)
    raise HTTPException(status_code=500, detail="Request is still being processed")


def check_rate_limit(email: str):
    """Raises 429 with a Retry-After header if this email is over 5 attempts per minute."""
    wait = supabase.rpc("check_rate_limit", {"p_email": email}).execute().data
    if wait and wait > 0:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many booking attempts for this email",
            headers={"Retry-After": str(wait)},
        )


# Helper functions
def encode_cursor(row: dict) -> str:
    """Pack created_at and id into one opaque, URL-safe string."""
    # Id as 16 raw bytes (base64 of this is ~22 chars instead of 36)
    id_part = base64.urlsafe_b64encode(uuid.UUID(row["id"]).bytes).decode().rstrip("=")
    raw = json.dumps({"c": row["created_at"], "i": id_part})
    return base64.urlsafe_b64encode(raw.encode()).decode()


def decode_cursor(cursor: str) -> tuple[str, str]:
    """Unpack a cursor. Any failure means the cursor is invalid -> 400."""
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        created_at = datetime.fromisoformat(data["c"])
        # Restore the base64 padding that was stripped, then rebuild the UUID
        id_bytes = base64.urlsafe_b64decode(data["i"] + "=" * (-len(data["i"]) % 4))
        booking_id = uuid.UUID(bytes=id_bytes)
        if created_at.tzinfo is None:
            raise ValueError("timestamp needs a timezone")
        return created_at.isoformat(), str(booking_id)
    except (ValueError, KeyError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid cursor"
        )

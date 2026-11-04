import os
import uuid
import re

from datetime import datetime, timezone

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Query, Request, status
from fastapi.exceptions import HTTPException, RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from starlette.exceptions import HTTPException as StarletteHTTPException
from database import supabase
from postgrest.exceptions import APIError

from schema import ResetRequest, SlotsResponse, SlotWithAvailability, NewBooking, Booking

# Extract X-API-KEY value
load_dotenv()
API_KEY = os.getenv("X_API_KEY")

# Custom api error definition
class CustomErrorException(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message

# Define X-API-Key header
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

# Validate its value
async def verify_api_key(key: str | None = Depends(api_key_header)):
    if key is None or key != API_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API Key",
        )

app = FastAPI(
    title="Clinic booking API",
    description="My API for Task 1 of the Koya Cohort 4 assessment. Every request needs the `X-API-Key` header (level 2)",
    dependencies=[Depends(verify_api_key)]   
)

# Add CustomErrorException exception to app
@app.exception_handler(CustomErrorException)
async def custom_error_exception_handler(request: Request, error: CustomErrorException):
    return JSONResponse(
        status_code=error.status_code,
        content={"error": f"{error.message}"}
    )

# Override default exceptions (400 and 401-499)
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    errors = exc.errors()
    message = errors[0]["msg"] if errors else "invalid request"
    return JSONResponse(
        status_code=400,
        content={"error": message},
    )

@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": f"{exc.detail}"}
    )
    
# Override any other unhandled exceptions that can lead to 500
@app.exception_handler(Exception)
async def universal_server_exception_handler(request: Request, exc: Exception):
    # log the real exc securely for easy debugging 
    print(f"Server error: {str(exc)}")
    
    return JSONResponse(
        status_code=500,
        content={"error": "Internal server error"}
    )


### api endpoints 

@app.post("/reset", summary="Level 1 · Replace all slots", response_description="Done")
async def replace_all_slots(slots: ResetRequest):
    """Deletes every booking and replaces all slots
    with the ones you send, including slots in the
    past."""
    try:
        # Convert each slot's time to ISO 8601 to conform with supabase
        payload = []
        for slot in slots.slots:
            payload.append(slot.model_dump(mode="json"))
        
        # Execute transaction
        supabase.rpc("reset_and_insert_slots", {"p_slots":payload}).execute()
        return {"message": "Done"}
    except APIError as e:
        # error code for duplicate ids
        print(f"PostgREST Error Code: {e.code}")
        print(f"Message: {e.message}")
        if (e.code == "23505"):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Creating duplicate ids is not allowed")
        else:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

@app.get("/slots", summary="Level 1 · List slots", response_description="The slots", response_model=SlotsResponse)
async def list_slots(provider: str = ""):
    """Lists every slot, earliest start first."""
    # Extract provider's slots based on provider filter: accepts something and nothing
    slots = supabase.table("slots").select("*")
    if provider != "":
        slots = slots.eq("provider", provider)
    slots_for_provider = slots.order("start_time").execute().data
    
    slots_list = SlotsResponse(slots=[])
    if slots_for_provider == []:
        return slots_list
    
    # Extract only booked slots (slots with a confirmed status)
    confirmed_slot_ids_table = supabase.table("bookings").select("slot_id").eq("status", "confirmed").execute().data 
    confirmed_slot_ids = {row["slot_id"] for row in confirmed_slot_ids_table}
    
    # Create slots list with availability status included [confirmed/cancelled]
    for slot in slots_for_provider:
        slot_availability = slot["id"] not in confirmed_slot_ids
        slots_list.slots.append(
            SlotWithAvailability(
                id=slot["id"],
                provider=slot["provider"],
                start=slot["start_time"],
                end=slot["end_time"],
                available=slot_availability
            )
        )
    
    return slots_list
    

@app.post("/bookings", summary="Level 1 – 5 · Book a slot", response_model=Booking, status_code=status.HTTP_201_CREATED,
          responses={
              201: {"description": "Booked"},
              400: {"description": "Missing or invalid field, or the slot has already started (level 2)"},
              404: {"description": "The slot doesn't exist (level 2)"}
          }
)
async def book_slot(new_booking: NewBooking):
    """
    **Level 1:** returns 201 with the booking, status `confirmed`.<br>
    **Level 2: 400** if `slot_id`, `name` or `email` is missing or empty, or the email isn't valid. **404** if the slot doesn't exist. **400** if the slot has already started. Trim spaces from `name` and `email`, and store `email` in lower case.
    """
    # Validation 1: check for missing or empty string fields
    is_missing_or_empty = (
        not hasattr(new_booking, "slot_id") or not hasattr(new_booking, "name") or not hasattr(new_booking, "email")
        or not str(new_booking.slot_id or "").strip()
        or not str(new_booking.name or "").strip()
        or not str(new_booking.email or "").strip()
    )
    if is_missing_or_empty:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="slot_id or name or email is missing or empty")
    
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
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="email is invalid")
 
    
    # Checks if a slot doesn't exist
    slot_exists = supabase.table("slots").select("*").eq("id", new_booking.slot_id).execute().data
    if slot_exists == []:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="slot doesn't exist")
 
    # Check if a slot has already started
    time_now = datetime.now(timezone.utc)  # noqa: UP017
    start_time = datetime.fromisoformat(slot_exists[0]["start_time"])
    
    if time_now >= start_time:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="slot has already started")
    
    # Add new booking to db
    new_booking_row = new_booking.model_dump(mode="json")
    new_booking_row["status"] = "confirmed"
    booking_row = supabase.table("bookings").insert(new_booking_row).execute().data[0]
    
    # Return Booking instance 
    booking = Booking.model_validate(booking_row)
    return booking


@app.get("/bookings", summary="Level 4 · List a person's bookings")
async def list_person_bookings(email: str, limit:int = Query(ge=1, le=50, default=10), cursor: str = ""):
    """That person's bookings, oldest first. The email
    match ignores case. Use cursor pagination: pass
    next_cursor back as cursor to get the next page.
    The last page has next_cursor: null."""
    pass


@app.get("/bookings/{id}", summary="Level 1 · Get a booking", response_model=Booking, responses={
    200: {"description": "The booking"},
    404: {"description": "No booking with that id"},
})
async def get_a_booking(id: str):
    # Ensure id is a valid supabase uuid
    try:
        uuid.UUID(id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="booking not found")
    
    # Get the booking
    booking = supabase.table("bookings").select("*").eq("id", id).execute().data
  
    # Check its content and return a result
    if booking != []:
        payload = Booking.model_validate(booking[0])
        return payload
    else:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="booking not found")
    

@app.delete("/bookings/{id}", summary="Level 3 · Cancel a booking")
async def cancel_a_booking(id: str):
    """Sets status to cancelled and frees the slot."""
    pass

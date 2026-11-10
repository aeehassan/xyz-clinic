import os
import uuid
from typing import Annotated

from dotenv import load_dotenv
from fastapi import APIRouter, Depends, FastAPI, Header, Query, Request, status
from fastapi.exceptions import HTTPException, RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from postgrest.exceptions import APIError
from starlette.exceptions import HTTPException as StarletteHTTPException

from database import supabase
from schema import (
    Booking,
    BookingsPage,
    Error,
    NewBooking,
    ResetRequest,
    SlotsResponse,
    SlotWithAvailability,
)
from utils import (
    check_rate_limit,
    clean_body,
    create_booking,
    decode_cursor,
    encode_cursor,
    release_key,
    replay,
    save_result,
)


# Custom api error definition
class CustomErrorException(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message


# Extract X-API-KEY value
load_dotenv()
API_KEY = os.getenv("X_API_KEY")


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
    dependencies=[Depends(verify_api_key)],
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)

router = APIRouter()


# Override default openapi docs
def custom_openapi():
    if not app.openapi_schema:
        app.openapi_schema = get_openapi(
            title=app.title,
            version=app.version,
            openapi_version=app.openapi_version,
            description=app.description,
            terms_of_service=app.terms_of_service,
            contact=app.contact,
            license_info=app.license_info,
            routes=app.routes,
            tags=app.openapi_tags,
            servers=app.servers,
        )
        for path, method_item in app.openapi_schema.get("paths").items():
            for method, param in method_item.items():
                # Make an exception for this endpoint
                if path == "/bookings" and method == "post":
                    continue  # Keep the 422 response and skip deletion

                responses = param.get("responses")
                # remove 422 response from all other endpoints
                if "422" in responses:
                    del responses["422"]
    return app.openapi_schema


app.openapi = custom_openapi


# {"error": "<message>"}
# Add CustomErrorException exception to app
@app.exception_handler(CustomErrorException)
async def custom_error_exception_handler(request: Request, error: CustomErrorException):
    return JSONResponse(
        status_code=error.status_code, content={"error": f"{error.message}"}
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
        content={"error": f"{exc.detail}"},
        headers=getattr(exc, "headers", None),  # lets Retry-After through
    )


# Override any other unhandled exceptions that can lead to 500
@app.exception_handler(Exception)
async def universal_server_exception_handler(request: Request, exc: Exception):
    # log the real exc securely for easy debugging
    print(f"Server error: {str(exc)}")

    return JSONResponse(status_code=500, content={"error": "Internal server error"})


### api endpoints


@router.post(
    "/reset", summary="Level 1 · Replace all slots", response_description="Done"
)
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
        supabase.rpc("reset_and_insert_slots", {"p_slots": payload}).execute()
        return {"message": "Done"}
    except APIError as e:
        # error code for duplicate ids
        print(f"PostgREST Error Code: {e.code}")
        print(f"Message: {e.message}")
        if e.code == "23505":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Creating duplicate ids is not allowed",
            )
        else:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)


@router.get(
    "/slots",
    summary="Level 1 · List slots",
    response_description="The slots",
    response_model=SlotsResponse,
)
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
    confirmed_slot_ids_table = (
        supabase.table("bookings")
        .select("slot_id")
        .eq("status", "confirmed")
        .execute()
        .data
    )
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
                available=slot_availability,
            )
        )

    return slots_list


@router.post(
    "/bookings",
    summary="Level 1 – 5 · Book a slot",
    response_model=Booking,
    status_code=status.HTTP_201_CREATED,
    responses={
        201: {"description": "Booked"},
        400: {
            "description": "Missing or invalid field, or the slot has already started (level 2)",
            "model": Error,
        },
        404: {"description": "The slot doesn't exist (level 2)", "model": Error},
        409: {"description": "Slot already booked (level 3)", "model": Error},
        422: {
            "description": "Idempotency-Key reused with a different body (level 4)",
            "model": Error,
        },
        429: {
            "description": "Too many attempts for this email (level 5)",
            "model": Error,
        },
    },
)
async def book_slot(
    new_booking: NewBooking, Idempotency_Key: Annotated[str, Header()] = None
):
    """
    **Level 1:** returns 201 with the booking, status `confirmed`.<br>
    **Level 2: 400** if `slot_id`, `name` or `email` is missing or empty,
    or the email isn't valid. **404** if the slot doesn't exist. **400**
    if the slot has already started. Trim spaces from `name` and `email`,
    and store `email` in lower case. <br>
    **Level 3:** a slot that already has a confirmed booking returns **409**.
    This must hold when many requests arrive at the same moment. <br>
    **Level 4:** the optional `Idempotency-Key` header. The same key with the same
    body returns the same response (same status code, same booking `id`) and
    never creates a second booking. The same key with a different body
    returns **422**. <br>
    **Level 5:** at most 5 attempts per email per minute, failed attempts
    included. The 6th returns **429** with a `Retry-After` header in whole seconds.
    Other emails aren't affected.
    """
    # Level 5: rate limit first, before idempotency and validation,
    # so a 429 is never saved as an idempotency response
    email_for_limit = (new_booking.email or "").strip().lower()
    if email_for_limit:  # no usable email means no one to count against
        check_rate_limit(email_for_limit)

    # If no key, behave like before
    if Idempotency_Key is None:
        return create_booking(new_booking)

    # Clean the body
    body = clean_body(new_booking)

    # Claim the key
    try:
        supabase.table("idempotency_keys").insert(
            {"key": Idempotency_Key, "request_body": body}
        ).execute()
    except APIError as e:
        if e.code == "23505":  # someone already claimed it
            return await replay(Idempotency_Key, body)
        print(f"PostgREST Error Code: {e.code}")
        print(f"Message: {e.message}")
        raise HTTPException(status_code=500, detail="Internal server error")

    # Create the booking and save its response to the
    # keys table
    try:
        booking = create_booking(new_booking)
    except HTTPException as exc:
        if exc.status_code >= 500:
            release_key(Idempotency_Key)
        else:
            save_result(Idempotency_Key, exc.status_code, {"error": exc.detail})
        raise
    except Exception:
        release_key(Idempotency_Key)
        raise

    save_result(Idempotency_Key, 201, booking.model_dump(mode="json"))
    return booking


@router.get(
    "/bookings",
    summary="Level 4 · List a person's bookings",
    response_model=BookingsPage,
    responses={
        200: {"description": "A page of bookings"},
        400: {"description": "Invalid limit", "model": Error},
    },
)
async def list_person_bookings(
    email: str,
    limit: int = Query(default=10, description="1 to 50. Anything else returns 400."),
    cursor: str = None,
):
    """That person's bookings, oldest first. The email
    match ignores case. Use cursor pagination: pass
    next_cursor back as cursor to get the next page.
    The last page has next_cursor: null."""
    # Normalise the email and validate limit
    email = email.strip().lower()
    if not 1 <= limit <= 50:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid limit"
        )

    # Base query for this person's bookings (any status)
    query = supabase.table("bookings").select("*").eq("email", email)
    # if there is a cursor, start at that bookmark.
    # "created_at later, OR same created_at and id >= bookmark id".
    # The id breaks ties between rows sharing the same timestamp.
    if cursor:
        created_at, booking_id = decode_cursor(cursor)
        query = query.or_(
            f"created_at.gt.{created_at},"
            f"and(created_at.eq.{created_at},id.gte.{booking_id})"
        )

    # oldest first, id as the tie-breaker, one extra row to detect a next page
    rows = query.order("created_at").order("id").limit(limit + 1).execute().data

    # extra row means there is another page
    next_cursor = None
    if len(rows) > limit:
        next_cursor = encode_cursor(rows[limit])  # first row of the NEXT page
        rows = rows[:limit]  # drop the extra row

    return BookingsPage(bookings=rows, next_cursor=next_cursor)


@router.get(
    "/bookings/{id}",
    summary="Level 1 · Get a booking",
    response_model=Booking,
    responses={
        200: {"description": "The booking"},
        404: {"description": "No booking with that id", "model": Error},
    },
)
async def get_a_booking(id: str):
    # Ensure id is a valid supabase uuid
    try:
        uuid.UUID(id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="booking not found"
        )

    # Get the booking
    booking = supabase.table("bookings").select("*").eq("id", id).execute().data

    # Check its content and return a result
    if booking != []:
        payload = Booking.model_validate(booking[0])
        return payload
    else:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="booking not found"
        )


@router.delete(
    "/bookings/{id}",
    summary="Level 3 · Cancel a booking",
    response_model=Booking,
    responses={
        200: {"description": "The cancelled booking"},
        404: {"description": "No booking with that id", "model": Error},
        409: {"description": "Already cancelled", "model": Error},
    },
)
async def cancel_a_booking(id: str):
    """Sets `status` to `cancelled` and frees the slot."""
    # Ensure id is a valid supabase uuid
    try:
        uuid.UUID(id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No booking with that id"
        )

    # Check if booking has been canceled
    is_cancelled = bool(
        supabase.table("bookings")
        .select("*")
        .eq("id", id)
        .eq("status", "cancelled")
        .execute()
        .data
    )
    if is_cancelled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Already cancelled"
        )

    # Cancel booking
    cancelled_booking = (
        supabase.table("bookings")
        .update({"status": "cancelled"})
        .eq("id", id)
        .execute()
        .data
    )
    if cancelled_booking == []:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No booking with that id"
        )

    # Return response
    booking = Booking.model_validate(cancelled_booking[0])
    return booking


app.include_router(router, prefix="/api")

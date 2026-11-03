import os

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Query, Request, status
from fastapi.exceptions import HTTPException, RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from starlette.exceptions import HTTPException as StarletteHTTPException
from database import supabase
from postgrest.exceptions import APIError

from schema import ResetRequest

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

@app.post("/reset", summary="Level 1 · Replace all slots")
async def replace_all_slots(slots: ResetRequest):
    """Deletes every booking and replaces all slots
    with the ones you send, including slots in the
    past."""
    try:
        # Convert each slot to a dict and get an array of jsons
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

@app.get("/slots", summary="Level 1 · List slots")
async def list_slots(provider: str = ""):
    """Lists every slot, earliest start first."""
    pass


@app.post("/bookings", summary="Level 1 – 5 · Book a slot")
async def book_slot():
    """...to be done at lvl 3"""
    pass


@app.get("/bookings", summary="Level 4 · List a person's bookings")
async def list_person_bookings(email: str, limit:int = Query(ge=1, le=50, default=10), cursor: str = ""):
    """That person's bookings, oldest first. The email
    match ignores case. Use cursor pagination: pass
    next_cursor back as cursor to get the next page.
    The last page has next_cursor: null."""
    pass


@app.get("/bookings/{id}", summary="Level 1 · Get a booking")
async def get_a_booking(id: str):
    pass


@app.delete("/bookings/{id}", summary="Level 3 · Cancel a booking")
async def cancel_a_booking(id: str):
    """Sets status to cancelled and frees the slot."""
    pass

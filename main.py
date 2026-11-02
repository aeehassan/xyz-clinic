from fastapi import FastAPI, Query, Request, status
from fastapi.responses import JSONResponse
from schema import Error


# Custom api error definition
class CustomErrorException(Exception):
    def __init__(self, message: str):
        self.status_code = self.status_code
        self.message = message

app = FastAPI(
    title="Clinic booking API",
    description="My API for Task 1 of the Koya Cohort 4 assessment.",
)

# Add exception to app
@app.exception_handler(CustomErrorException)
async def custom_error_exception_handler(request: Request, error: CustomErrorException):
    return JSONResponse(
        status_code=error.status_code,
        content={"error": f"{error.message}"}
    )


@app.post("/reset")
async def replace_all_slots():
    """Deletes every booking and replaces all slots
    with the ones you send, including slots in the
    past."""
    pass


@app.get("/slots")
async def list_slots(provider: str = ""):
    """Lists every slot, earliest start first."""
    pass


@app.post("/bookings")
async def book_slot():
    """...to be done at lvl 3"""
    pass


@app.get("/bookings")
async def list_person_bookings(email: str, limit:int = Query(ge=1, le=50, default=10), cursor: str = ""):
    """That person's bookings, oldest first. The email
    match ignores case. Use cursor pagination: pass
    next_cursor back as cursor to get the next page.
    The last page has next_cursor: null."""
    pass


@app.get("/bookings/{id}")
async def get_a_booking(id: str):
    pass


@app.delete("/bookings/{id}")
async def cancel_a_booking(id: str):
    """Sets status to cancelled and frees the slot."""
    pass

# Install the only third-party dependency with: pip install httpx
"""Black-box grader for the Clinic Booking API.

Usage:
    python checker.py --base-url BASE_URL --api-key API_KEY [--level LEVEL]
        [--verbose] [--timeout SECONDS] [--race-n N] [--burst N] [--yes]

Required parameters:
    --base-url URL   API base URL. If omitted, uses the BASE_URL environment
                     variable. Trailing slashes are removed.
    --api-key KEY    Value for the X-API-Key header. If omitted, uses the
                     API_KEY environment variable.

Optional parameters:
    --level LEVEL    Levels to run: all (default), one level from 1 to 5
                     (for example 3), or an ascending range (for example 1-3).
    --verbose        Print each request's method, path, status, and duration.
    --timeout SEC    Timeout for each HTTP request in seconds (default: 30).
    --race-n N       Concurrent requests in each Level 3 race (default: 20).
    --burst N        Booking requests in the Level 5 rate-limit test
                     (default: 50).
    --yes            Confirm running against a non-local target. The checker
                     calls POST /reset, which wipes all target data.

Example:
    python checker.py --base-url https://my-api.onrender.com --api-key MYKEY \
        --level 1-3 --verbose --timeout 30 --race-n 20 --yes

For a local API, --yes is not required. The script requires httpx; install it
with: pip install httpx


"""

import argparse
import os
import re
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

import httpx

BOOKING_FIELDS = {"id", "slot_id", "name", "email", "status", "created_at"}
SLOT_FIELDS = {"id", "provider", "start", "end", "available"}
NO_KEY = object()


class CheckFailure(Exception):
    pass


class Checker:
    def __init__(self, base_url, api_key, timeout, verbose):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.verbose = verbose
        self.client = httpx.Client(timeout=timeout)
        self.passed = 0
        self.failed = 0

    def close(self):
        self.client.close()

    def request(
        self,
        method,
        path,
        *,
        json_body=None,
        content=None,
        params=None,
        key=None,
        extra_headers=None,
    ):
        headers = {}
        selected_key = self.api_key if key is None else key
        if selected_key is not NO_KEY:
            headers["X-API-Key"] = selected_key
        if extra_headers:
            headers.update(extra_headers)
        started = time.perf_counter()
        try:
            response = self.client.request(
                method,
                f"{self.base_url}{path}",
                json=json_body,
                content=content,
                params=params,
                headers=headers,
            )
        except httpx.HTTPError as error:
            elapsed = time.perf_counter() - started
            if self.verbose:
                print(f"{method} {path} ERROR {elapsed:.3f}s: {error}")
            raise CheckFailure(f"{method} {path} request failed: {error}") from error
        elapsed = time.perf_counter() - started
        if self.verbose:
            print(f"{method} {path} {response.status_code} {elapsed:.3f}s")
        return response

    @staticmethod
    def json(response):
        try:
            return response.json()
        except ValueError as error:
            raise CheckFailure("response was not valid JSON") from error

    @classmethod
    def error_body(cls, response):
        body = cls.json(response)
        if (
            not isinstance(body, dict)
            or set(body) != {"error"}
            or not isinstance(body["error"], str)
        ):
            raise CheckFailure(f"expected {{'error': '<message>'}}, got {body!r}")
        return body

    def expect(self, condition, message):
        if not condition:
            raise CheckFailure(message)

    def run(self, label, check):
        try:
            check()
        except CheckFailure as error:
            self.failed += 1
            print(f"FAIL  {label}: {error}")
        except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError) as error:
            self.failed += 1
            print(f"FAIL  {label}: unexpected {type(error).__name__}: {error}")
        else:
            self.passed += 1
            print(f"PASS  {label}")

    @staticmethod
    def make_slots(count, prefix="slot"):
        base = datetime.now(UTC) + timedelta(days=30)
        return [
            {
                "id": f"{prefix}-{uuid.uuid4().hex[:8]}-{index}",
                "provider": f"Dr Checker {index % 3}",
                "start": (base + timedelta(minutes=45 * index))
                .isoformat()
                .replace("+00:00", "Z"),
                "end": (base + timedelta(minutes=45 * index + 30))
                .isoformat()
                .replace("+00:00", "Z"),
            }
            for index in range(count)
        ]

    def reset(self, slots):
        response = self.request("POST", "/reset", json_body={"slots": slots})
        self.expect(
            response.status_code == 200,
            f"POST /reset returned {response.status_code}, expected 200",
        )

    def slot_list(self):
        response = self.request("GET", "/slots")
        self.expect(
            response.status_code == 200, f"GET /slots returned {response.status_code}"
        )
        body = self.json(response)
        self.expect(
            isinstance(body, dict) and isinstance(body.get("slots"), list),
            "GET /slots must return {slots: [...]}",
        )
        return body["slots"]

    def book(self, slot_id, name, email, *, key=None, idempotency_key=None):
        headers = (
            {"Idempotency-Key": idempotency_key}
            if idempotency_key is not None
            else None
        )
        return self.request(
            "POST",
            "/bookings",
            json_body={"slot_id": slot_id, "name": name, "email": email},
            key=key,
            extra_headers=headers,
        )

    def booking_body(self, response):
        body = self.json(response)
        self.expect(
            isinstance(body, dict) and BOOKING_FIELDS <= body.keys(),
            f"booking missing fields: {body!r}",
        )
        self.expect(
            isinstance(body["id"], str) and bool(body["id"]),
            "booking id must be a non-empty string",
        )
        self.expect(
            body["status"] in {"confirmed", "cancelled"},
            "booking status must be confirmed or cancelled",
        )
        self.expect(
            isinstance(body["slot_id"], str), "booking slot_id must be a string"
        )
        self.expect(
            isinstance(body["name"], str) and isinstance(body["email"], str),
            "booking name/email must be strings",
        )
        self.parse_datetime(body["created_at"], "created_at")
        return body

    @staticmethod
    def parse_datetime(value, label):
        if not isinstance(value, str):
            raise CheckFailure(f"{label} must be an ISO date-time string")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as error:
            raise CheckFailure(f"{label} is not an ISO date-time: {value!r}") from error
        if parsed.tzinfo is None:
            raise CheckFailure(f"{label} must include a timezone")
        return parsed

    @staticmethod
    def future_email(prefix):
        return f"{prefix}-{uuid.uuid4().hex[:10]}@example.com"

    def verify_slots(self, slots, expected_available=None):
        for item in slots:
            self.expect(
                isinstance(item, dict) and SLOT_FIELDS <= item.keys(),
                f"slot missing required fields: {item!r}",
            )
            self.expect(isinstance(item["id"], str), "slot id must be a string")
            self.expect(
                isinstance(item["provider"], str), "slot provider must be a string"
            )
            self.expect(
                isinstance(item["available"], bool), "slot available must be a boolean"
            )
            self.parse_datetime(item["start"], "slot start")
            self.parse_datetime(item["end"], "slot end")
            if expected_available is not None:
                self.expect(
                    item["available"] is expected_available.get(item["id"]),
                    f"unexpected availability for {item['id']}",
                )

    def preflight(self):
        response = self.request("GET", "/slots")
        self.expect(
            response.status_code == 200,
            f"GET /slots returned {response.status_code}; check URL and API key",
        )
        body = self.json(response)
        self.expect(
            isinstance(body, dict) and isinstance(body.get("slots"), list),
            "GET /slots returned an unexpected response",
        )

    def level1(self):
        slots = self.make_slots(3, "l1")
        self.run("L1 reset replaces slots", lambda: self.reset(slots))

        def verify_listing():
            listed = self.slot_list()
            self.verify_slots(listed, {slot["id"]: True for slot in slots})
            self.expect(
                {slot["id"] for slot in listed} == {slot["id"] for slot in slots},
                "reset slot set did not match GET /slots",
            )
            starts = [
                self.parse_datetime(item["start"], "slot start") for item in listed
            ]
            self.expect(
                starts == sorted(starts),
                "GET /slots is not ordered earliest start first",
            )

        self.run("L1 slot list shape, types, ordering, availability", verify_listing)
        payload = {
            "slot_id": slots[0]["id"],
            "name": "Ada Obi",
            "email": "ada@example.com",
        }
        booking = {}

        def create_booking():
            response = self.request("POST", "/bookings", json_body=payload)
            self.expect(
                response.status_code in {200, 201},
                f"POST /bookings returned {response.status_code}",
            )
            body = self.booking_body(response)
            self.expect(
                body["status"] == "confirmed", "new booking status was not confirmed"
            )
            self.expect(
                body["slot_id"] == payload["slot_id"],
                "booking returned the wrong slot_id",
            )
            self.expect(
                body["name"] == payload["name"] and body["email"] == payload["email"],
                "booking did not echo name/email",
            )
            booking.update(body)

        self.run("L1 create a booking", create_booking)

        def lookup_booking():
            self.expect(
                bool(booking), "booking creation failed, so lookup cannot be checked"
            )
            response = self.request("GET", f"/bookings/{booking['id']}")
            self.expect(
                response.status_code == 200,
                f"GET booking returned {response.status_code}",
            )
            self.expect(
                self.booking_body(response) == booking,
                "GET booking did not return the same booking",
            )

        self.run("L1 retrieve the created booking", lookup_booking)

        def consumed_slot():
            listed = self.slot_list()
            self.verify_slots(
                listed, {slot["id"]: slot["id"] != payload["slot_id"] for slot in slots}
            )

        self.run("L1 booking updates slot availability", consumed_slot)

        def missing_booking():
            response = self.request("GET", f"/bookings/unknown-{uuid.uuid4().hex}")
            self.expect(
                response.status_code == 404,
                f"unknown booking returned {response.status_code}, expected 404",
            )
            self.error_body(response)

        self.run("L1 unknown booking returns 404 JSON error", missing_booking)

        def unknown_slot():
            response = self.book(
                f"unknown-{uuid.uuid4().hex}", "Ada Obi", self.future_email("missing")
            )
            self.expect(
                response.status_code == 404,
                f"unknown slot returned {response.status_code}, expected 404",
            )
            self.error_body(response)

        self.run("L1 unknown slot returns 404 JSON error", unknown_slot)

        def persists():
            self.expect(
                bool(booking),
                "booking creation failed, so persistence cannot be checked",
            )
            time.sleep(0.2)
            self.slot_list()
            response = self.request("GET", f"/bookings/{booking['id']}")
            self.expect(
                response.status_code == 200,
                f"booking disappeared after other requests: {response.status_code}",
            )
            self.expect(
                self.booking_body(response) == booking,
                "booking changed after other requests",
            )

        self.run("L1 booking persists across requests", persists)

    def level2(self):
        slots = self.make_slots(2, "l2")
        self.run("L2 reset test fixture", lambda: self.reset(slots))
        unknown_id = f"unknown-{uuid.uuid4().hex}"
        valid_payload = {
            "slot_id": slots[0]["id"],
            "name": "Valid Name",
            "email": self.future_email("l2auth"),
        }
        endpoints = [
            ("POST", "/reset", {"slots": slots}, None),
            ("GET", "/slots", None, None),
            ("POST", "/bookings", valid_payload, None),
            ("GET", "/bookings", None, {"email": valid_payload["email"]}),
            ("GET", f"/bookings/{unknown_id}", None, None),
            ("DELETE", f"/bookings/{unknown_id}", None, None),
        ]

        def authentication():
            for method, path, body, params in endpoints:
                for supplied_key in (NO_KEY, "definitely-wrong-key", ""):
                    response = self.request(
                        method, path, json_body=body, params=params, key=supplied_key
                    )
                    self.expect(
                        response.status_code == 401,
                        f"{method} {path} with {supplied_key!r} returned {response.status_code}, expected 401",
                    )
                    self.error_body(response)

        self.run(
            "L2 missing, wrong, and empty key rejected on every endpoint",
            authentication,
        )

        def validation():
            invalid_payloads = []
            for field in ("slot_id", "name", "email"):
                invalid_payloads.append(
                    (
                        f"missing {field}",
                        {
                            key: value
                            for key, value in valid_payload.items()
                            if key != field
                        },
                    )
                )
            for value, label in (("", "empty name"), ("   ", "whitespace name")):
                invalid_payloads.append((label, {**valid_payload, "name": value}))
            for email in ("abc", "a@", "@b.com", "a b@c.com", ""):
                invalid_payloads.append(
                    (f"invalid email {email!r}", {**valid_payload, "email": email})
                )
            invalid_payloads.extend(
                [
                    ("empty slot_id", {**valid_payload, "slot_id": ""}),
                    ("numeric name", {**valid_payload, "name": 42}),
                    ("list email", {**valid_payload, "email": ["a@example.com"]}),
                    ("integer slot_id", {**valid_payload, "slot_id": 42}),
                ]
            )
            for label, payload in invalid_payloads:
                response = self.request("POST", "/bookings", json_body=payload)
                self.expect(
                    response.status_code in {400, 422},
                    f"{label} returned {response.status_code}, expected 400 or 422",
                )
                self.error_body(response)
            for label, content in (("non-JSON body", b"not json"), ("empty body", b"")):
                response = self.request(
                    "POST",
                    "/bookings",
                    content=content,
                    extra_headers={"Content-Type": "application/json"},
                )
                self.expect(
                    response.status_code in {400, 422},
                    f"{label} returned {response.status_code}, expected 400 or 422",
                )
                self.error_body(response)
            response = self.request("POST", "/bookings", json_body=[])
            self.expect(
                response.status_code in {400, 422},
                f"JSON array returned {response.status_code}, expected 400 or 422",
            )
            self.error_body(response)

        self.run("L2 booking input validation", validation)

        def no_consumption():
            listed = self.slot_list()
            self.verify_slots(listed, {slot["id"]: True for slot in slots})

        self.run("L2 invalid requests do not consume a slot", no_consumption)

        def normalization():
            name = "  Ada Obi  "
            email = "  Ada.Trim@example.com  "
            response = self.book(slots[1]["id"], name, email)
            self.expect(
                response.status_code in {200, 201},
                f"trimmed valid booking returned {response.status_code}",
            )
            body = self.booking_body(response)
            self.expect(
                body["name"] == name.strip(), f"name was not trimmed: {body['name']!r}"
            )
            self.expect(
                body["email"] == email.strip().lower(),
                f"email was not trimmed and lower-cased: {body['email']!r}",
            )

        self.run("L2 booking trims name and lower-cases email", normalization)

    def parallel_requests(self, count, operation):
        barrier = threading.Barrier(count)

        def run_one(index):
            try:
                barrier.wait(timeout=self.timeout)
                return operation(index)
            except (CheckFailure, threading.BrokenBarrierError) as error:
                return error

        with ThreadPoolExecutor(max_workers=count) as executor:
            return list(executor.map(run_one, range(count)))

    def level3(self, race_n):
        slots = self.make_slots(7, "l3")
        self.run("L3 reset test fixture", lambda: self.reset(slots))
        first = self.book(slots[0]["id"], "First Patient", self.future_email("l3first"))
        first_booking = {}

        def booked_conflict():
            self.expect(
                first.status_code in {200, 201},
                f"initial booking returned {first.status_code}",
            )
            first_booking.update(self.booking_body(first))
            response = self.book(
                slots[0]["id"], "Second Patient", self.future_email("l3second")
            )
            self.expect(
                response.status_code == 409,
                f"booking an occupied slot returned {response.status_code}, expected 409",
            )
            self.error_body(response)

        self.run("L3 confirmed slot rejects another booking", booked_conflict)

        def race_test():
            race_slots = slots[1:6]
            for slot in race_slots:
                emails = [self.future_email(f"race{index}") for index in range(race_n)]
                results = self.parallel_requests(
                    race_n,
                    lambda index, slot_id=slot["id"], race_emails=emails: self.book(
                        slot_id, f"Racer {index}", race_emails[index]
                    ),
                )
                self.expect(
                    all(isinstance(result, httpx.Response) for result in results),
                    f"request exception in race for {slot['id']}",
                )
                codes = [result.status_code for result in results]
                self.expect(
                    codes.count(201) + codes.count(200) == 1,
                    f"race for {slot['id']} had status codes {codes}",
                )
                self.expect(
                    codes.count(409) == race_n - 1,
                    f"race for {slot['id']} did not return 409 for all losers: {codes}",
                )
                for response in results:
                    if response.status_code in {200, 201}:
                        winner = self.booking_body(response)
                        self.expect(
                            winner["status"] == "confirmed",
                            "race winner was not confirmed",
                        )
                current = {item["id"]: item["available"] for item in self.slot_list()}
                self.expect(
                    current.get(slot["id"]) is False,
                    f"raced slot {slot['id']} is still available",
                )
                confirmed_count = 0
                for email in emails:
                    listed = self.list_bookings(email)
                    self.expect(
                        listed.status_code == 200,
                        f"GET /bookings for race participant returned {listed.status_code}",
                    )
                    body = self.json(listed)
                    self.expect(
                        isinstance(body, dict)
                        and isinstance(body.get("bookings"), list),
                        "race participant list must contain bookings[]",
                    )
                    confirmed_count += sum(
                        item.get("status") == "confirmed"
                        and item.get("slot_id") == slot["id"]
                        for item in body["bookings"]
                    )
                self.expect(
                    confirmed_count == 1,
                    f"raced slot has {confirmed_count} confirmed bookings, expected exactly one",
                )

        self.run(f"L3 {race_n}-request races on five slots", race_test)

        def cancel_and_rebook():
            self.expect(
                bool(first_booking), "initial booking was unavailable for cancellation"
            )
            response = self.request("DELETE", f"/bookings/{first_booking['id']}")
            self.expect(
                response.status_code == 200,
                f"DELETE booking returned {response.status_code}",
            )
            cancelled = self.booking_body(response)
            self.expect(
                cancelled["status"] == "cancelled",
                "DELETE did not return a cancelled booking",
            )
            fetched = self.request("GET", f"/bookings/{first_booking['id']}")
            self.expect(
                fetched.status_code == 200
                and self.booking_body(fetched)["status"] == "cancelled",
                "cancelled status was not persisted",
            )
            available = {item["id"]: item["available"] for item in self.slot_list()}
            self.expect(
                available.get(slots[0]["id"]) is True,
                "cancelled slot did not become available",
            )
            rebook = self.book(
                slots[0]["id"], "Another Patient", self.future_email("l3rebook")
            )
            self.expect(
                rebook.status_code in {200, 201},
                f"booking a cancelled slot returned {rebook.status_code}",
            )
            self.expect(
                self.booking_body(rebook)["status"] == "confirmed",
                "rebooking did not create a confirmed booking",
            )

        self.run("L3 cancellation frees the slot for rebooking", cancel_and_rebook)

        def cancel_errors():
            self.expect(
                bool(first_booking),
                "initial booking was unavailable for cancellation checks",
            )
            repeated = self.request("DELETE", f"/bookings/{first_booking['id']}")
            self.expect(
                repeated.status_code == 409,
                f"repeat cancellation returned {repeated.status_code}, OpenAPI specifies 409",
            )
            self.error_body(repeated)
            missing = self.request("DELETE", f"/bookings/unknown-{uuid.uuid4().hex}")
            self.expect(
                missing.status_code == 404,
                f"unknown cancellation returned {missing.status_code}, expected 404",
            )
            self.error_body(missing)

        self.run("L3 repeat and unknown cancellation codes", cancel_errors)

        def cancel_rebook_race():
            fresh_slots = self.make_slots(1, "l3cancelrace")
            self.reset(fresh_slots)
            initial = self.book(
                fresh_slots[0]["id"], "Original", self.future_email("cancelrace")
            )
            self.expect(
                initial.status_code in {200, 201},
                f"race setup booking returned {initial.status_code}",
            )
            initial_body = self.booking_body(initial)
            new_email = self.future_email("cancelrace")

            def cancel(_):
                return self.request("DELETE", f"/bookings/{initial_body['id']}")

            def rebook(_):
                return self.book(fresh_slots[0]["id"], "Replacement", new_email)

            results = self.parallel_requests(
                2, lambda index: cancel(index) if index == 0 else rebook(index)
            )
            self.expect(
                all(isinstance(result, httpx.Response) for result in results),
                f"cancel/rebook race raised: {results!r}",
            )
            cancel_response, book_response = results
            self.expect(
                cancel_response.status_code == 200,
                f"concurrent cancellation returned {cancel_response.status_code}",
            )
            self.expect(
                book_response.status_code in {200, 201, 409},
                f"concurrent rebooking returned {book_response.status_code}",
            )
            if book_response.status_code == 409:
                self.error_body(book_response)
            original_after = self.request("GET", f"/bookings/{initial_body['id']}")
            self.expect(
                original_after.status_code == 200,
                "original booking disappeared after race",
            )
            original_status = self.booking_body(original_after)["status"]
            confirmed = int(original_status == "confirmed")
            if book_response.status_code in {200, 201}:
                confirmed += int(
                    self.booking_body(book_response)["status"] == "confirmed"
                )
            self.expect(
                confirmed <= 1, "cancel/rebook race left two confirmed bookings"
            )
            available = {item["id"]: item["available"] for item in self.slot_list()}
            self.expect(
                available.get(fresh_slots[0]["id"]) is (confirmed == 0),
                "slot availability disagrees with confirmed booking state",
            )

        self.run("L3 concurrent cancel and rebook stays consistent", cancel_rebook_race)

    def list_bookings(self, email, *, limit=None, cursor=None):
        params = {"email": email}
        if limit is not None:
            params["limit"] = limit
        if cursor is not None:
            params["cursor"] = cursor
        return self.request("GET", "/bookings", params=params)

    def level4(self):
        slots = self.make_slots(17, "l4")
        self.run("L4 reset test fixture", lambda: self.reset(slots))

        def retry_safety():
            payload = {
                "slot_id": slots[0]["id"],
                "name": "Idempotent Patient",
                "email": self.future_email("idem"),
            }
            key = str(uuid.uuid4())
            first = self.book(**payload, idempotency_key=key)
            second = self.book(**payload, idempotency_key=key)
            self.expect(
                first.status_code in {200, 201},
                f"first idempotent request returned {first.status_code}",
            )
            self.expect(
                second.status_code == first.status_code,
                f"retry returned status {second.status_code}, first was {first.status_code}",
            )
            first_body = self.booking_body(first)
            second_body = self.booking_body(second)
            self.expect(
                first_body["id"] == second_body["id"] and first_body == second_body,
                "retry did not return the same booking",
            )
            listed = self.list_bookings(payload["email"])
            self.expect(
                listed.status_code == 200,
                f"GET /bookings returned {listed.status_code}",
            )
            page = self.json(listed)
            self.expect(
                isinstance(page, dict) and isinstance(page.get("bookings"), list),
                "booking list must contain bookings[]",
            )
            self.expect(
                sum(item.get("id") == first_body["id"] for item in page["bookings"])
                == 1,
                "idempotent retry created duplicate bookings",
            )

        self.run("L4 idempotent retry returns the original booking", retry_safety)

        def concurrent_idempotency():
            payload = {
                "slot_id": slots[1]["id"],
                "name": "Concurrent Patient",
                "email": self.future_email("idemrace"),
            }
            key = str(uuid.uuid4())
            results = self.parallel_requests(
                5, lambda _: self.book(**payload, idempotency_key=key)
            )
            self.expect(
                all(isinstance(result, httpx.Response) for result in results),
                f"idempotency race raised: {results!r}",
            )
            self.expect(
                all(result.status_code in {200, 201} for result in results),
                f"idempotency race statuses: {[result.status_code for result in results]}",
            )
            self.expect(
                len({result.status_code for result in results}) == 1,
                "concurrent retries returned different status codes",
            )
            bodies = [self.booking_body(result) for result in results]
            self.expect(
                len({body["id"] for body in bodies}) == 1,
                "concurrent retry created multiple booking IDs",
            )
            self.expect(
                all(body == bodies[0] for body in bodies),
                "concurrent retries returned different booking bodies",
            )

        self.run("L4 five concurrent same-key retries", concurrent_idempotency)

        def different_body_same_key():
            key = str(uuid.uuid4())
            email = self.future_email("idemdifferent")
            first = self.book(slots[2]["id"], "First Body", email, idempotency_key=key)
            self.expect(
                first.status_code in {200, 201},
                f"initial idempotent request returned {first.status_code}",
            )
            changed = self.book(
                slots[3]["id"], "Different Body", email, idempotency_key=key
            )
            self.expect(
                changed.status_code == 422,
                f"same key with different body returned {changed.status_code}, expected 422",
            )
            self.error_body(changed)

        self.run(
            "L4 idempotency key cannot be reused with a different body",
            different_body_same_key,
        )

        def different_keys_conflict():
            first = self.book(
                slots[4]["id"],
                "First",
                self.future_email("keyone"),
                idempotency_key=str(uuid.uuid4()),
            )
            self.expect(
                first.status_code in {200, 201},
                f"first booking returned {first.status_code}",
            )
            second = self.book(
                slots[4]["id"],
                "Second",
                self.future_email("keytwo"),
                idempotency_key=str(uuid.uuid4()),
            )
            self.expect(
                second.status_code == 409,
                f"different key on occupied slot returned {second.status_code}, expected 409",
            )
            self.error_body(second)

        self.run(
            "L4 different idempotency keys do not bypass slot conflict",
            different_keys_conflict,
        )

        def pagination():
            email = self.future_email("pagination")
            created = []
            for index, slot in enumerate(slots[5:17]):
                response = self.book(slot["id"], f"Page Patient {index}", email)
                if response.status_code == 429:
                    error_body = self.error_body(response)
                    retry_after = response.headers.get("Retry-After")
                    wait = (
                        int(retry_after) + 1
                        if retry_after and retry_after.isdigit()
                        else 61
                    )
                    print(
                        f"INFO  L4 pagination hit rate limit ({error_body['error']}); waiting {wait}s"
                    )
                    time.sleep(wait)
                    response = self.book(slot["id"], f"Page Patient {index}", email)
                self.expect(
                    response.status_code in {200, 201},
                    f"pagination booking {index} returned {response.status_code}",
                )
                created.append(self.booking_body(response))
            self.expect(
                len(created) == 12, "pagination fixture did not create 12 bookings"
            )

            def fetch(cursor=None):
                response = self.list_bookings(email, limit=5, cursor=cursor)
                self.expect(
                    response.status_code == 200,
                    f"booking page returned {response.status_code}",
                )
                body = self.json(response)
                self.expect(
                    isinstance(body, dict) and isinstance(body.get("bookings"), list),
                    "page response must contain bookings[]",
                )
                self.expect(
                    "next_cursor" in body, "page response is missing next_cursor"
                )
                self.expect(
                    body["next_cursor"] is None or isinstance(body["next_cursor"], str),
                    "next_cursor must be a string or null",
                )
                return body

            first_page = fetch()
            self.expect(
                len(first_page["bookings"]) == 5,
                f"page 1 had {len(first_page['bookings'])} items, expected 5",
            )
            self.expect(
                first_page["next_cursor"] is not None,
                "page 1 should have a next_cursor",
            )
            second_page = fetch(first_page["next_cursor"])
            self.expect(
                len(second_page["bookings"]) == 5,
                f"page 2 had {len(second_page['bookings'])} items, expected 5",
            )
            self.expect(
                second_page["next_cursor"] is not None,
                "page 2 should have a next_cursor",
            )
            third_page = fetch(second_page["next_cursor"])
            self.expect(
                len(third_page["bookings"]) == 2,
                f"page 3 had {len(third_page['bookings'])} items, expected 2",
            )
            self.expect(
                third_page["next_cursor"] is None,
                "last page should have next_cursor: null",
            )
            combined = (
                first_page["bookings"]
                + second_page["bookings"]
                + third_page["bookings"]
            )
            ids = [item.get("id") for item in combined]
            self.expect(
                len(ids) == len(set(ids)) == 12,
                "pagination has duplicate or missing booking IDs",
            )
            self.expect(
                set(ids) == {item["id"] for item in created},
                "pagination omitted or introduced bookings",
            )
            timestamps = [
                self.parse_datetime(item["created_at"], "booking created_at")
                for item in combined
            ]
            self.expect(
                timestamps == sorted(timestamps),
                "pagination ordering is not oldest first",
            )
            repeated = fetch()
            self.expect(
                [item.get("id") for item in repeated["bookings"]]
                == [item.get("id") for item in first_page["bookings"]],
                "pagination ordering is unstable",
            )
            own_only = self.list_bookings(self.future_email("notowner"), limit=5)
            self.expect(
                own_only.status_code == 200,
                f"other person's list returned {own_only.status_code}",
            )
            own_body = self.json(own_only)
            self.expect(
                own_body.get("bookings") == [],
                "one person can see another person's bookings",
            )

            for limit in (0, -1, "abc", 51, 1000000):
                invalid = self.list_bookings(email, limit=limit)
                self.expect(
                    invalid.status_code == 400,
                    f"invalid limit {limit!r} returned {invalid.status_code}, expected 400 per OpenAPI",
                )
                self.error_body(invalid)
            print(
                "INFO  page=0 is not exercised: the OpenAPI contract specifies cursor pagination, not a page parameter"
            )
            print(
                "INFO  beyond-last-page cursor is not synthesized: next_cursor is null and cursor values are opaque"
            )

        self.run(
            "L4 cursor pagination, ordering, ownership, and invalid limits", pagination
        )

    def level5(self, burst):
        slots = self.make_slots(burst, "l5")
        self.run("L5 reset test fixture", lambda: self.reset(slots))
        email = self.future_email("spam")
        responses = []

        def burst_test():
            for index, slot in enumerate(slots):
                response = self.book(slot["id"], f"Burst {index}", email)
                responses.append((slot["id"], response))
                if response.status_code == 429:
                    self.error_body(response)
                    break
            rejected = [
                (slot_id, response)
                for slot_id, response in responses
                if response.status_code == 429
            ]
            accepted = sum(
                response.status_code in {200, 201} for _, response in responses
            )
            print(
                f"INFO  Level 5 burst accepted {accepted} booking(s) before first 429"
            )
            if rejected:
                retry_after = rejected[0][1].headers.get("Retry-After")
                print(
                    f"INFO  Retry-After: {retry_after if retry_after is not None else 'not returned'}"
                )
                self.expect(
                    retry_after is None or retry_after.isdigit(),
                    "Retry-After must be a whole number of seconds",
                )
            self.expect(
                bool(rejected), f"no 429 received in {len(responses)} booking attempts"
            )
            rejected_slots = {slot_id for slot_id, _ in rejected}
            listed = self.list_bookings(email, limit=50)
            self.expect(
                listed.status_code == 200,
                f"GET /bookings after burst returned {listed.status_code}",
            )
            listed_body = self.json(listed)
            self.expect(
                isinstance(listed_body, dict)
                and isinstance(listed_body.get("bookings"), list),
                "booking list must contain bookings[]",
            )
            created_slots = {item.get("slot_id") for item in listed_body["bookings"]}
            self.expect(
                not (rejected_slots & created_slots), "a 429 response created a booking"
            )
            for slot_id, response in rejected:
                self.expect(response.status_code == 429, "unexpected burst status")
                self.error_body(response)
                available = {item["id"]: item["available"] for item in self.slot_list()}
                self.expect(
                    available.get(slot_id) is True,
                    f"429 request consumed slot {slot_id}",
                )

        self.run(f"L5 rate limit blocks a burst of {burst}", burst_test)

        def reads_not_limited():
            for _ in range(3):
                response = self.request("GET", "/slots")
                self.expect(
                    response.status_code == 200,
                    f"GET /slots was rate limited: {response.status_code}",
                )

        self.run("L5 booking limiter does not block slot reads", reads_not_limited)

        def recovery():
            rejected = next(
                (response for _, response in responses if response.status_code == 429),
                None,
            )
            if rejected is None:
                raise CheckFailure(
                    "no 429 response, so Retry-After recovery cannot be checked"
                )
            retry_after = rejected.headers.get("Retry-After")
            wait = int(retry_after) + 1 if retry_after and retry_after.isdigit() else 61
            print(f"INFO  waiting {wait}s before checking rate-limit recovery")
            time.sleep(wait)
            used_slots = {slot_id for slot_id, _ in responses}
            remaining = next(
                (slot for slot in slots if slot["id"] not in used_slots), None
            )
            if remaining is None:
                remaining = self.make_slots(1, "l5recovery")[0]
                self.reset([remaining])
            response = self.book(remaining["id"], "Legitimate Patient", email)
            self.expect(
                response.status_code in {200, 201},
                f"request after rate-limit wait returned {response.status_code}",
            )

        self.run("L5 booking succeeds after the rate-limit window", recovery)


def parse_levels(value):
    if value == "all":
        return [1, 2, 3, 4, 5]
    if re.fullmatch(r"[1-5]", value):
        return [int(value)]
    match = re.fullmatch(r"([1-5])-([1-5])", value)
    if match:
        start, end = map(int, match.groups())
        if start <= end:
            return list(range(start, end + 1))
    raise argparse.ArgumentTypeError(
        "level must be 'all', a number 1-5, or an ascending range such as 1-3"
    )


def is_local_url(base_url):
    hostname = (urlparse(base_url).hostname or "").lower()
    return hostname in {"localhost", "127.0.0.1", "::1"}


def main():
    parser = argparse.ArgumentParser(
        description="Black-box grader for the Clinic Booking API"
    )
    parser.add_argument(
        "--base-url", default=None, help="API URL (or BASE_URL environment variable)"
    )
    parser.add_argument(
        "--api-key", default=None, help="X-API-Key (or API_KEY environment variable)"
    )
    parser.add_argument(
        "--level",
        type=parse_levels,
        default=parse_levels("all"),
        metavar="LEVEL",
        help="all, one level (3), or an ascending range (1-3); default: all",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="print method, path, status, and time for each request",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30,
        help="per-request timeout in seconds (default: 30)",
    )
    parser.add_argument(
        "--race-n", type=int, default=20, help="requests per race test (default: 20)"
    )
    parser.add_argument(
        "--burst",
        type=int,
        default=50,
        help="requests in the Level 5 spam test (default: 50)",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="confirm that the target may be wiped by POST /reset",
    )
    args = parser.parse_args()

    base_url = (
        (args.base_url if args.base_url is not None else os.getenv("BASE_URL", ""))
        .strip()
        .rstrip("/")
    )
    api_key = args.api_key if args.api_key is not None else os.getenv("API_KEY", "")
    if not base_url:
        parser.error("--base-url or BASE_URL is required")
    if not api_key:
        parser.error("--api-key or API_KEY is required and must be non-empty")
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    if args.race_n < 2:
        parser.error("--race-n must be at least 2")
    if args.burst < 1:
        parser.error("--burst must be at least 1")

    print(
        "WARNING: this checker uses POST /reset, which wipes all data on the target API."
    )
    if not is_local_url(base_url) and not args.yes:
        print("Refusing to continue against a non-local URL without --yes.")
        return 1

    checker = Checker(base_url, api_key, args.timeout, args.verbose)
    try:
        try:
            checker.preflight()
        except CheckFailure as error:
            print(f"Preflight failed: {error}")
            return 1
        print("Preflight passed: API is reachable and the API key was accepted.")

        for level in args.level:
            print(f"\nLevel {level}")
            if level == 1:
                checker.level1()
            elif level == 2:
                checker.level2()
            elif level == 3:
                checker.level3(args.race_n)
            elif level == 4:
                checker.level4()
            else:
                checker.level5(args.burst)
    finally:
        checker.close()

    print(f"\nSummary: {checker.passed} passed, {checker.failed} failed")
    return 0 if checker.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

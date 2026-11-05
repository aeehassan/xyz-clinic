## Overview

This is a clinic booking system made of a simple web page and an API. Patients can view available appointment slots, book one, see a confirmation, and cancel or look up their bookings. It is deployed on Vercel.

## Database

The project uses a Supabase (PostgreSQL) database with two tables.

### slots

| Field | Type | Notes |
|---|---|---|
| id | text | Primary key |
| provider | text | Required |
| start_time | timestamp with time zone (0) | Required |
| end_time | timestamp with time zone (0) | Required |

### bookings

| Field | Type | Notes |
|---|---|---|
| id | uuid | Primary key, generated automatically |
| slot_id | text | Required, foreign key to `slots.id` (deleting a slot deletes its bookings) |
| name | text | Required |
| email | text | Required |
| status | text | Required, either `confirmed` or `cancelled`, defaults to `confirmed` |
| created_at | timestamp with time zone (0) | Required, set automatically |

### Relationship

One slot can have multiple bookings over time. And each booking belongs to exactly one slot.

### Why this design

The design avoids update anomalies (changing a provider in many places) and deletion anomalies (losing slot info when a booking is removed), while the foreign key and status check keep the data consistent as the app grows.

### To add

- A rule so a slot can have only one confirmed booking at a time, while cancelled bookings don't count. This stops double booking even when requests arrive at the same moment.
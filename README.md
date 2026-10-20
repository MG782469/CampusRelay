# CampusRelay

CampusRelay is a deterministic, evidence-first campus event workflow prototype. It turns a bounded natural-language event request into a human-approved booking in a **local synthetic campus reservation ledger**. It does not connect to a real university system.

## Implemented V2 workflow

1. Create a request from a supported sentence format.
2. Plan and validate event name, room, date, attendees, and optional start/end times.
3. Submit the current plan for approval.
4. Approve with one of the seeded synthetic advisors (`advisor-lee` / Dr. Lee or `advisor-patel` / Dr. Patel).
5. Execute an atomic SQLite reservation after checking room existence, availability, capacity, and interval overlap.
6. Read the reservation back from SQLite and only then mark the workflow completed.
7. Display reservation, approval, operation, verification, and audit evidence in the browser.

Conflicting requests receive deterministic available-room alternatives. CampusRelay never silently changes a student's room or time; a changed plan increments its plan version and invalidates the previous approval.

## Synthetic campus boundary

The seeded rooms are local records only:

| Room | Building | Capacity | Equipment |
|---|---|---:|---|
| Student Union room 101 | Student Union | 40 | projector, whiteboard |
| Library room 201 | Library | 80 | projector, hybrid camera |
| Engineering room 301 | Engineering | 120 | projector, lab tables |

The campus timezone assumption is **UTC** for persisted timestamps; event dates and times are stored as the submitted local wall-clock values. This prototype has synthetic identities, not production authentication.

## Persistence schema

SQLite is used through Python's standard library. The database contains:

- `rooms`: room identity, building, capacity, availability, and equipment.
- `advisors`: controlled demo advisor identities.
- `requests`: request text, normalized plan, state, plan version, approval, audit, evidence, and reservation reference.
- `reservations`: unique reservation ID, request ID, room, event name, date, time range, attendee count, status, operation ID, and creation timestamp.
- `operations`: idempotency responses keyed by logical operation ID.

The reservation transaction uses `BEGIN IMMEDIATE` and checks `start_time < existing_end AND end_time > existing_start`, so adjacent intervals are allowed while overlaps are rejected. `CAMPUSRELAY_DB` selects the database path; the default is an in-memory database for isolated development. Set it to a writable file for restart recovery.

## API routes

- `POST /api/requests` with `{ "text": "..." }`
- `POST /api/requests/{id}/plan`
- `POST /api/requests/{id}/submit`
- `POST /api/requests/{id}/approve` with synthetic advisor identity and `role: "advisor"`
- `POST /api/requests/{id}/execute` with optional stable `operation_id`
- `GET /api/requests/{id}`
- `GET /api/requests/{id}/alternatives`
- `GET /api/rooms`
- `GET /api/advisors`

Execution is idempotent for a repeated operation ID and returns the existing ledger result instead of creating a second reservation. Completed workflows cannot be reopened or executed again.

## Run locally

Requires Python 3.12, pinned in `.python-version`. No third-party packages are required.

```sh
export CAMPUSRELAY_DB=./campusrelay.sqlite3
python app.py
# open http://localhost:8000
```

## Tests

The reproducible offline command is:

```sh
make test
```

The test suite uses temporary SQLite databases and covers the original six workflow contracts plus availability, overlap, adjacent intervals, capacity, unknown rooms, alternatives, approval invalidation, transition guards, idempotency, evidence, parser behavior, and restart persistence.

`pytest` is not required or installed; use `make test` / `unittest` as documented.

## Docker

The image contains the pinned Python runtime and project files. The default command runs the offline suite:

```sh
docker build -t campusrelay .
docker run --rm campusrelay
```

To run the server with a writable mounted database:

```sh
docker run --rm -p 8000:8000 -e CAMPUSRELAY_DB=/tmp/campusrelay.sqlite3 --entrypoint python campusrelay app.py
```

## Reproducible demo

Use the browser's default request for Scenario A: plan, submit, approve as Dr. Lee, execute, and inspect the green evidence panel. For Scenario B, create another request for Student Union room 101 on `2026-11-05` from `15:00` to `17:00`; execution returns a conflict and lists Library room 201 as a suitable alternative. To demonstrate Scenario C, change the request explicitly to the suggested room, re-plan, submit, approve again, then execute. Repeating the same execution operation ID demonstrates Scenario D without a duplicate reservation.

## Limitations

- The parser intentionally supports a narrow deterministic format; ambiguous or missing input must be supplied explicitly.
- Advisor identity is a controlled synthetic demo gate, not secure authentication.
- No real university booking, calendar, email, or identity integration exists.
- This prototype does not implement a distributed lock service; SQLite transaction locking is sufficient for its local single-database scope.
- Reservations are synthetic and should not be represented as institutional confirmation.

## License

MIT; see `LICENSE`.

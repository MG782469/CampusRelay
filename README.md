# CampusRelay

CampusRelay is a deterministic, evidence-first campus event workflow prototype. It turns a bounded natural-language event request into a human-approved booking in a **local synthetic campus reservation ledger**. It does not connect to a real university system.

## Implemented V3 workflow foundation

1. Create a request from a supported sentence format.
2. Plan and validate all required fields: event name, room, date, start time, end time, attendees, and advisor.
3. Submit the current plan for approval.
4. Approve with one of the seeded synthetic advisors (`advisor-lee` / Dr. Lee or `advisor-patel` / Dr. Patel).
5. Execute an atomic SQLite reservation after checking room existence, availability, capacity, and interval overlap.
6. Read the reservation back from SQLite and only then mark the workflow completed.
7. Display reservation, approval, operation, verification, and audit evidence in the browser.

8. Evaluate the canonical SQLite room inventory and confirmed reservations with a deterministic, read-only constraint planner.
9. Apply hard capacity, equipment, enabled-room, and interval constraints; rank feasible rooms using explicit preferences and stable tie-breaks.
10. Expose structured candidates, facts, rejected-room reasons, flexible-time evaluation, and the recommended proposal without treating planning as authorization.

Conflicting requests receive deterministic available-room alternatives. CampusRelay never silently changes a student's room or time. Re-planning without a material change preserves the plan version and approval; material edits increment the version and invalidate approval. Milestone 2 planning is a proposal only; Milestone 3 will add explicit same-workflow alternative selection and renewed approval.

Attendance must be a strict positive integer (not a boolean, fraction, or coerced numeric string). Accepted times are normalized to `HH:MM` before comparison; start must precede end on the same date. Missing or invalid required fields block submission.

## Synthetic campus boundary

The seeded rooms are local records only:

| Room | Building | Capacity | Equipment |
|---|---|---:|---|
| Student Union room 101 | Student Union | 40 | projector, whiteboard |
| Library room 201 | Library | 80 | projector, hybrid camera |
| Engineering room 301 | Engineering | 120 | projector, lab tables |

New and explicitly revalidated plans use the synthetic campus's fixed **UTC+05:30** offset, with no daylight-saving transitions or timezone database dependency. Audit timestamps are UTC. Historical event wall-clock values are preserved, never implicitly migrated or reinterpreted on startup; explicitly revalidate an older plan before relying on the new timezone contract.

Synthetic advisor identities and roles are caller-supplied and spoofable. This demo approval gate is **not real authentication or authorization**.

## Persistence schema

SQLite is used through Python's standard library. The database contains:

- `rooms`: room identity, building, capacity, availability, and equipment.
- `advisors`: controlled demo advisor identities.
- `requests`: request text, normalized plan, state, plan version, approval, audit, evidence, and reservation reference.
- `reservations`: unique reservation ID, request ID, room, event name, date, time range, attendee count, status, operation ID, and creation timestamp.
- `operations`: idempotency responses keyed by a global operation ID, with workflow/action/payload metadata for conflict detection.

Planning is read-only and uses the same `rooms` and confirmed `reservations` tables. It does not persist a reservation or bypass approval. New request data may contain `required_equipment`, `preferred_room`, `preferred_building`, `advisor_required`, and an explicitly supplied `flexible_time_window`.

Schema upgrades are additive: operation metadata and verification metadata support replay and recovery without destructively recreating existing tables or rewriting historical wall clocks. Verification evidence is recorded only after reading the committed reservation back. A workflow left in `verifying` after a committed reservation can recover by verifying that existing reservation instead of making a second booking.

The reservation transaction uses `BEGIN IMMEDIATE` and checks `start_time < existing_end AND end_time > existing_start`, so adjacent intervals are allowed while overlaps are rejected. `CAMPUSRELAY_DB` selects the database path; the default is an in-memory database for isolated development. Set it to a writable file for restart recovery.

## API routes

- `POST /api/requests` with `{ "text": "..." }`
- `POST /api/requests/{id}/plan`
- `POST /api/requests/{id}/submit`
- `POST /api/requests/{id}/approve` with synthetic advisor identity and `role: "advisor"`
- `POST /api/requests/{id}/execute` with optional stable `operation_id`
- `GET /api/requests/{id}`
- `GET /api/requests/{id}/alternatives`
- `GET /api/requests/{id}/plan` for a read-only structured planning proposal
- `GET /api/rooms`
- `GET /api/advisors`

Operation keys are global, not scoped independently to each workflow or route. Repeating a key for the same workflow, action, and payload replays its recorded result; using it with a conflicting workflow, action, actor, plan version, or payload is rejected. Successful execution replay does not create another reservation. Completed workflows cannot be reopened or freshly executed with a new key.

## Run locally

Requires **Python 3.12.15** and GNU Make on your PATH. `.python-version` and `Dockerfile` pin the same patch release. `requirements.lock` intentionally contains no dependencies: the application and tests use only the standard library, so no `pip install` is needed.

The pin was verified against the [official Python 3.12.15 release](https://www.python.org/downloads/release/python-31215/) (September 30, 2026) and the [official Python image source](https://github.com/docker-library/python/blob/master/3.12/slim-bookworm/Dockerfile). This security release is source-only; it has no official Windows installer. Use the Docker path below on a clean machine without that runtime.

From the repository root, with Python 3.12.15 already installed:

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -c "import sys; assert sys.version_info[:3] == (3, 12, 15), sys.version"
export CAMPUSRELAY_DB=./data/campusrelay.sqlite3
export PORT=8000
python app.py
# open http://localhost:8000
```

PowerShell, if the pinned runtime is available through the Python launcher:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -c "import sys; assert sys.version_info[:3] == (3, 12, 15), sys.version"
$env:CAMPUSRELAY_DB = './data/campusrelay.sqlite3'
$env:PORT = '8000'
.\.venv\Scripts\python.exe app.py
```

`.env.example` documents the variables; the application does not automatically load `.env`. The server listens on `0.0.0.0`; `PORT` defaults to `8000`. A positional port (`python app.py 8080`) overrides `PORT`. Without `CAMPUSRELAY_DB`, host runs use an ephemeral in-memory ledger. A file path enables persistence and its parent directory is created as needed. Stop the server with Ctrl+C.

## Tests

The single offline test command (host or built image) is:

```sh
make test
```

`make test` overrides inherited `CAMPUSRELAY_DB` with `:memory:` before test imports. Test cases use isolated temporary databases; no demo database or running application's ledger is required. No dependency download or external service is needed. The suite covers workflow transitions, validation, booking conflicts, approval, replay, evidence, and persistence.

On PowerShell with GNU Make available, use `make test PYTHON=.venv/Scripts/python.exe`. Without Make, the equivalent standard-library invocation is:

```powershell
$env:CAMPUSRELAY_DB = ':memory:'
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p 'test_*.py' -v
```

On Linux with unprivileged user/network namespaces enabled, enforce a disconnected host test with `unshare --user --map-root-user --net make test`. Restricted hosts may disallow that command; the Docker `--network none` command below is the isolated alternative. `pytest` is not required.

## Docker

Requires Docker Engine or Docker Desktop using Linux containers. Build from the repository root; this step needs network access for the base image and Debian's `make` package. No application dependency installation or compile step is needed. The default container command starts the application, not the test suite:

```sh
docker build --pull -t campusrelay:m1 .
docker run --rm -p 127.0.0.1:8000:8000 campusrelay:m1
```

Open `http://localhost:8000`. This run's database is disposable when the container is removed. For durable storage, mount a named volume at the image's database directory:

```sh
docker volume create campusrelay-data
docker run --rm --name campusrelay -p 127.0.0.1:8000:8000 --mount type=volume,source=campusrelay-data,target=/data -e CAMPUSRELAY_DB=/data/campusrelay.sqlite3 campusrelay:m1
```

Re-running the same command reuses the ledger; `--rm` removes the container, not the named volume. These single-line Docker commands also work in PowerShell. To change ports, replace the mapping with `-p 127.0.0.1:8080:8080` and add `-e PORT=8080`.

After building, run the same test target with networking disabled and without mounting your data volume:

```sh
docker run --rm --network none campusrelay:m1 make test
```

The image includes GNU Make and the tests. Build context exclusions cover SQLite databases and sidecars, environment/credential files (except `.env.example`), caches, and `.dist`; explicit `COPY` instructions include only application/test inputs. Python is patch-pinned, but the base image tag and Debian package repositories are not immutable digest/snapshot locks, so builds are not claimed to be byte-for-byte identical. No remote deployment is configured in Milestone 1.

## Reproducible demo

Use the browser's default request for Scenario A: plan, submit, approve as Dr. Lee, execute, and inspect the green evidence panel. For Scenario B, create another request for Student Union room 101 on `2026-11-05` from `15:00` to `17:00`; execution returns a conflict and lists Library room 201 as a suitable alternative. To demonstrate Scenario C, change the request explicitly to the suggested room, re-plan, submit, approve again, then execute. Repeating the same execution operation ID demonstrates Scenario D without a duplicate reservation.

## Limitations

- The parser and planner intentionally support a bounded deterministic format; ambiguous or missing input produces clarification requirements rather than guesses.
- Planning is deterministic constraint evaluation, not runtime LLM reasoning.
- Equipment identifiers are limited to the seeded synthetic inventory (`projector`, `whiteboard`, `hybrid_camera`, and `lab_tables`).
- Advisor identity is a spoofable synthetic demo gate, not secure authentication.
- No LLM, real university booking, calendar, email, or identity integration exists.
- This prototype does not implement a distributed lock service; SQLite transaction locking is sufficient for its local single-database scope.
- Reservations are synthetic and should not be represented as institutional confirmation.

## License

MIT; see `LICENSE`.

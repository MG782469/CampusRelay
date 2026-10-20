# CampusRelay

**CampusRelay turns a student's vague event request into a verified, human-approved campus workflow.**

This is a focused Frontier Buildathon prototype for the **Agents & Automation** track. It uses a synthetic campus environment and makes no claim to submit to a real university system.

## Problem and users

Student organization coordinators often move an event request between chat, a room-booking form, an advisor, and a separate confirmation email. Missing details are discovered late, approvals are hard to audit, and “done” often means only that someone sent a message.

CampusRelay is for student coordinators and the advisor/staff members who approve their events. It accepts a natural-language request, identifies missing intake data, creates a deterministic plan, enforces an advisor approval gate, runs a synthetic reservation tool, and records completion evidence.

## Why this is agentic

The orchestrator performs a bounded workflow rather than answering a question: interpret intent, extract structured fields, plan steps, ask for missing information, enforce authorization, execute tools, recover from a simulated tool outage, and verify an observable result. Predictable policy checks are deterministic and testable offline; a future approved structured-output model can replace only the interpretation step.

## MVP workflow

1. Enter: “Book Student Union room 101 for the Robotics Club workshop on 2026-11-05 from 15:00 to 17:00 for 20 attendees; advisor Dr. Lee.”
2. CampusRelay creates a plan and shows its state transitions.
3. Missing information blocks submission instead of being invented.
4. Only a payload with `role: advisor` can approve.
5. Execution produces a synthetic reservation reference and completion evidence.
6. A simulated tool failure is recorded as `failed` and can be safely retried.

## Architecture

```text
Browser UI (index.html)
        │ JSON over HTTP
Python stdlib HTTP server (app.py)
        ├── deterministic intent/field parser
        ├── explicit request state machine
        ├── approval and idempotency gates
        ├── synthetic reservation tool + recovery path
        └── audit log + evidence records
```

State is in-memory by design for the demo. The synthetic campus boundary is explicit: `SYN-ROOM-2026-1105-101` is not a real reservation.

## Run locally

Requires Python 3.12 (see `.python-version`). No third-party packages are required.

```sh
python app.py
# open http://localhost:8000
```

## Test and Docker

The single offline test command is:

```sh
make test
```

It runs six `unittest` cases covering the happy path, missing information, authorization, idempotent approval, tool failure/recovery, and completion evidence.

```sh
docker build -t campusrelay .
docker run --rm -p 8000:8000 campusrelay
```

The Docker image runs the offline tests by default. To run the server in a container:

```sh
docker run --rm -p 8000:8000 --entrypoint python campusrelay app.py
```

## Research and differentiation

Three candidate areas were compared before selection: hostel maintenance, placement eligibility, and student-event approvals. Maintenance products such as service-desk portals already cover ticket routing; placement systems already cover opportunity listings and eligibility; event tools cover room calendars and forms. The selected opportunity is the fragmented handoff **between** a natural-language student request, policy prerequisites, advisor approval, and verifiable room-booking evidence. CampusRelay is not a chatbot or another form: its distinctive MVP is a small, inspectable state machine with approval enforcement, failure recovery, and evidence-first closure.

## Safety and limitations

- Synthetic data only; no real student records or institutional integrations.
- No automatic impersonation or external communication.
- Human approval is required before execution.
- The parser is intentionally narrow and deterministic for reproducibility.
- State is lost when the process stops; production would use authenticated users, durable storage, encrypted audit records, and real campus APIs with least-privilege scopes.

## Buildathon disclosure

Development was performed through the OmniRush coding workflow. The repository is prepared for public review, but publication, dashboard qualification, and final Unstop submission are separate actions and are not claimed here.

## License

MIT; see `LICENSE`.

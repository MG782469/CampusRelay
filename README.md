# CampusRelay contract tests

This repository contains a Python `unittest` contract suite for the planned
CampusRelay app. The application implementation is intentionally not included;
the suite expects an `app.py` module exporting:

```python
create_app_state()
handle_request(state, method, path, payload=None)
```

`handle_request` returns `(status_code, body)`, with `body` as a JSON-like
dictionary. The tests define the request lifecycle, status codes, response
fields, advisor authorization, idempotent approval, simulated execution
failure/recovery, and completion evidence.

## Run

Requires Python 3.12 (see `.python-version`). From the project directory:

```sh
make test
```

The suite will remain red until `app.py` is implemented. To run it in the
reproducible container:

```sh
docker build -t campusrelay-tests .
docker run --rm campusrelay-tests
```

## Contract assumptions

- Event creation accepts natural language in `{"text": "..."}` and returns
  HTTP 201 with an id and an initial `draft` or `needs_information` status.
- Planning returns deterministic `steps` and a `missing_fields` list.
- Submitting incomplete data returns HTTP 422 and accepts missing values in a
  subsequent submit payload, such as `{"location": "Student Union room 101"}`.
- Approval accepts an advisor payload with `role: "advisor"`; unauthorized
  approval returns HTTP 403.
- Approval is idempotent: repeating the same approval does not append another
  audit event.
- Execution accepts `simulate_failure`; a simulated failure returns HTTP 502,
  exposes recovery information, and can be retried successfully.
- A completed GET response includes `status`, `steps`, `audit`, and non-empty
  `evidence`.

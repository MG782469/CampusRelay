"""Validated synthetic booking policy and crash-recoverable SQLite orchestration.

The advisor IDs are synthetic identities, not authentication credentials. New or
explicitly revalidated plans use a fixed UTC+05:30 offset (no DST inference).
Legacy rows are never rewritten by schema migration or a read.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import urlparse

from planner import normalize_planning_requirements, plan_request

POLICY_ZONE = "UTC+05:30"
POLICY_TZ = timezone(timedelta(hours=5, minutes=30))
FIELDS = {"event_name", "location", "date", "attendees", "start_time", "end_time", "advisor", "required_equipment", "preferred_room", "preferred_building", "advisor_required", "flexible_time_window"}
REQUIRED = ("event_name", "location", "date", "attendees", "start_time", "end_time", "advisor")
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
TIME_RE = re.compile(r"([01]?[0-9]|2[0-3]):[0-5][0-9]\Z")
OMITTED = object()


def now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


@contextmanager
def transaction(state):
    """Rollback on every exit, including a simulated process-termination exception."""
    state.conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        state.conn.execute("COMMIT")
    finally:
        if state.conn.in_transaction:
            state.conn.execute("ROLLBACK")


def migrate(state):
    additions = {
        "reservations": {"start_at": "TEXT", "end_at": "TEXT", "timezone": "TEXT", "plan_hash": "TEXT", "plan_version": "INTEGER", "booking_snapshot": "TEXT"},
        "operations": {"actor": "TEXT", "plan_version": "INTEGER", "input_hash": "TEXT", "booking_hash": "TEXT", "phase": "TEXT NOT NULL DEFAULT 'done'"},
    }
    with transaction(state):
        for table, columns in additions.items():
            existing = {row["name"] for row in state.conn.execute(f"PRAGMA table_info({table})")}
            for name, declaration in columns.items():
                if name not in existing:
                    state.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")


def public(row):
    if isinstance(row, dict):
        return deepcopy(row)
    result = {"id": row["id"], "request_id": row["id"], "text": row["text"], "status": row["status"], "plan_version": row["plan_version"]}
    for field, column in (("data", "data"), ("steps", "steps"), ("missing_fields", "missing"), ("audit", "audit"), ("evidence", "evidence"), ("approval", "approval")):
        result[field] = json.loads(row[column]) if row[column] is not None else None
    for field in ("recovery", "reservation_id"):
        if row[field]:
            result[field] = row[field]
    return result


def load(state, rid):
    row = state.conn.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
    return public(row) if row else None


def save(state, req):
    state.conn.execute("UPDATE requests SET data=?,status=?,steps=?,missing=?,audit=?,evidence=?,approval=?,plan_version=?,recovery=?,reservation_id=? WHERE id=?", (canonical(req["data"]), req["status"], canonical(req["steps"]), canonical(req["missing_fields"]), canonical(req["audit"]), canonical(req["evidence"]), canonical(req["approval"]) if req["approval"] else None, req["plan_version"], req.get("recovery"), req.get("reservation_id"), req["id"]))


def record(req, action, actor="system", detail=""):
    req["audit"].append({"at": now(), "action": action, "actor": actor, "detail": detail})


def error(status, message, req=None, **extra):
    body = public(req) if req else {}
    body.update(extra)
    body.update(error=message, error_code={400: "invalid_request", 403: "forbidden", 404: "not_found", 405: "method_not_allowed", 409: "conflict", 422: "validation_error", 502: "execution_failed", 503: "storage_unavailable"}.get(status, "internal_error"))
    return status, body


def advisor_id(value):
    if not isinstance(value, str):
        return None
    value = value.strip().lower().rstrip(".")
    for surname in ("lee", "patel"):
        if value == "advisor-" + surname or re.fullmatch(r"dr\.?\s+" + surname, value):
            return "advisor-" + surname
    return None


def parse(text):
    data = {}
    patterns = {"date": r"\b([0-9]{4}-[0-9]{2}-[0-9]{2})\b", "start_time": r"\b([0-9]{1,2}:[0-9]{2})\s*(?:to|[-–])", "end_time": r"(?:to|[-–])\s*([0-9]{1,2}:[0-9]{2})\b", "attendees": r"\bfor\s+([0-9]+)\s+attendees?\b"}
    for key, pattern in patterns.items():
        match = re.search(pattern, text, re.I)
        if match:
            data[key] = int(match[1]) if key == "attendees" else match[1]
    room = re.search(r"((?:student union|library|engineering)\s+room\s+[0-9]+|room\s+[0-9]+)", text, re.I)
    if room:
        data["location"] = room[1].strip()
    preferred_room = re.search(r"(?:prefer|preferred room(?: is)?|preference is)\s+((?:student union|library|engineering)\s+room\s+[0-9]+|room\s+[0-9]+)", text, re.I)
    if preferred_room:
        data["preferred_room"] = preferred_room[1].strip()
    preferred_building = re.search(r"(?:prefer|preferred)\s+(?:the\s+)?(student union|library|engineering)(?:\s+building)?\b", text, re.I)
    if preferred_building:
        data["preferred_building"] = preferred_building[1].strip()
    equipment = []
    for item, pattern in (("projector", r"\bprojector\b"), ("whiteboard", r"\bwhiteboard\b"), ("hybrid_camera", r"\bhybrid\s+camera\b"), ("lab_tables", r"\blab\s+tables?\b")):
        if re.search(pattern, text, re.I) and re.search(r"(?:must|need|require|with|equipped|equipment)", text, re.I):
            equipment.append(item)
    if equipment:
        data["required_equipment"] = equipment
    advisor = re.search(r"\b(advisor-(?:lee|patel)|Dr\.?\s+(?:Lee|Patel))\b", text, re.I)
    if advisor:
        data["advisor"] = advisor_id(advisor[1])
        data["advisor_required"] = True
    event = re.search(r"(?:for|about)\s+(?:the\s+)?(.+?)(?:\s+on\s+[0-9]{4}|\s+at\s+|\s+workshop|\s+event|[.;]|$)", text, re.I)
    if event:
        data["event_name"] = event[1].strip().title()
    return data


def normalize(data):
    """Return a new canonical plan; never mutate a stored historical snapshot."""
    out = {key: value for key, value in data.items() if key in FIELDS}
    errors = []
    for key, value in list(out.items()):
        if key == "attendees":
            if type(value) is not int or value <= 0:
                errors.append("attendees must be a positive integer")
        elif key == "required_equipment":
            continue
        elif key == "advisor_required":
            if type(value) is not bool:
                errors.append("advisor_required must be a boolean")
        elif key == "flexible_time_window":
            continue
        elif not isinstance(value, str) or not value.strip():
            errors.append(f"{key} must be a nonempty string")
        else:
            out[key] = value.strip()
    if errors:
        return out, errors
    if "date" in out:
        try:
            if not DATE_RE.fullmatch(out["date"]):
                raise ValueError()
            date.fromisoformat(out["date"])
        except ValueError:
            errors.append("date must be a valid calendar date in YYYY-MM-DD format")
    for key in ("start_time", "end_time"):
        if key in out:
            if not TIME_RE.fullmatch(out[key]):
                errors.append(f"{key} must use H:MM or HH:MM in 24-hour time")
            else:
                hour, minute = map(int, out[key].split(":"))
                out[key] = time(hour, minute).strftime("%H:%M")
    if not errors and "start_time" in out and "end_time" in out:
        if time.fromisoformat(out["start_time"]) >= time.fromisoformat(out["end_time"]):
            errors.append("end_time must be after start_time on the same date")
    if "advisor" in out:
        resolved = advisor_id(out["advisor"])
        if resolved is None:
            errors.append("advisor must be Dr. Lee, Dr. Patel, advisor-lee, or advisor-patel")
        else:
            out["advisor"] = "Dr. Lee" if resolved == "advisor-lee" else "Dr. Patel"
            out["advisor_id"] = resolved
    planning_fields = {key: out[key] for key in ("event_name", "date", "start_time", "end_time", "attendees", "required_equipment", "preferred_room", "preferred_building", "advisor_required", "flexible_time_window") if key in out}
    if out.get("advisor_id"):
        planning_fields["assigned_advisor"] = out["advisor_id"]
    planning, planning_errors = normalize_planning_requirements(planning_fields)
    errors.extend(item["message"] for item in planning_errors)
    for key in ("required_equipment", "preferred_room", "preferred_building", "advisor_required", "flexible_time_window"):
        if key in planning:
            out[key] = planning[key]
    out["timezone"] = POLICY_ZONE
    if not errors and all(out.get(key) for key in ("date", "start_time", "end_time")):
        for clock, stamp in (("start_time", "start_at"), ("end_time", "end_at")):
            out[stamp] = datetime.combine(date.fromisoformat(out["date"]), time.fromisoformat(out[clock]), POLICY_TZ).isoformat()
    return out, errors


def schema(action, payload):
    if type(payload) is not dict:
        return None, ["JSON body must be an object"]
    common = {"operation_id", "operationId", "actor"}
    allowed = {"create": {"text"}, "plan": FIELDS | {"advisor_id"} | common, "submit": FIELDS | {"advisor_id"} | common, "approve": {"advisor_id", "role", "plan_version"} | common, "execute": {"simulate_failure", "plan_version"} | common}.get(action, set())
    errors = [f"Unknown field: {key}" for key in payload if key not in allowed]
    out = dict(payload)
    for key, value in payload.items():
        if key not in allowed:
            continue
        if key in ("attendees", "plan_version"):
            if type(value) is not int or value < (1 if key == "attendees" else 0):
                errors.append(f"{key} must be {'a positive' if key == 'attendees' else 'a nonnegative'} integer")
        elif key == "required_equipment":
            if type(value) is not list or any(not isinstance(item, str) or not item.strip() for item in value):
                errors.append("required_equipment must be a list of nonempty strings")
        elif key == "advisor_required":
            if type(value) is not bool:
                errors.append("advisor_required must be a boolean")
        elif key == "flexible_time_window":
            if type(value) is not dict:
                errors.append("flexible_time_window must be an object")
        elif key == "simulate_failure":
            if type(value) is not bool:
                errors.append("simulate_failure must be a boolean")
        elif not isinstance(value, str) or not value.strip():
            errors.append(f"{key} must be a nonempty string")
        else:
            out[key] = value.strip()
    if errors:
        return None, errors
    if "operationId" in out:
        if "operation_id" in out and out["operation_id"] != out["operationId"]:
            return None, ["operation_id and operationId must match"]
        out["operation_id"] = out.pop("operationId")
    if action in ("plan", "submit"):
        if "advisor_id" in out:
            if "advisor" in out and advisor_id(out["advisor"]) != advisor_id(out["advisor_id"]):
                return None, ["advisor and advisor_id must identify the same advisor"]
            out["advisor"] = out.pop("advisor_id")
    if action == "create" and not out.get("text"):
        errors.append("Describe the event you need to coordinate.")
    if action == "approve":
        for key in ("advisor_id", "actor"):
            if key in out:
                out[key] = advisor_id(out[key]) or out[key]
        if "actor" in out and "advisor_id" in out and out["actor"] != out["advisor_id"]:
            errors.append("actor and advisor_id must identify the same advisor")
    return out, errors


def steps(data):
    result = [{"id": "intake", "label": "Understand request", "status": "complete"}, {"id": "policy", "label": "Check campus policy", "status": "complete"}, {"id": "room", "label": "Reserve the requested space", "status": "ready"}, {"id": "advisor", "label": "Obtain advisor approval", "status": "blocked"}, {"id": "verify", "label": "Verify reservation evidence", "status": "pending"}]
    if data.get("attendees", 0) > 50:
        result.append({"id": "safety", "label": "Route safety review for large event", "status": "ready"})
    return sorted(result, key=lambda item: item["id"])


def booking_hash(req):
    return digest(req["data"])


def planning_requirements(data):
    requirements = {key: data[key] for key in ("event_name", "date", "start_time", "end_time", "attendees", "required_equipment", "preferred_building", "advisor_required", "flexible_time_window") if key in data}
    if data.get("advisor_id"):
        requirements["assigned_advisor"] = data["advisor_id"]
    if data.get("preferred_room"):
        requirements["preferred_room"] = data["preferred_room"]
    elif data.get("location"):
        requirements["preferred_room"] = data["location"]
    return requirements


def planning_result(state, req):
    return plan_request(state, planning_requirements(req["data"]))


def assigned_advisor(state, data):
    return state.conn.execute("SELECT * FROM advisors WHERE id=? AND active=1 AND role='advisor'", (advisor_id(data.get("advisor")),)).fetchone()


def room_for(state, location):
    rooms = state.conn.execute("SELECT * FROM rooms ORDER BY id").fetchall()
    location = location.strip().lower()
    exact = [room for room in rooms if location in (room["id"].lower(), room["name"].lower())]
    matches = exact or [room for room in rooms if room["name"].lower().endswith(" " + location)]
    return matches[0] if len(matches) == 1 else None


def overlaps(state, room_id, data, exclude_request=None):
    # Parse historical clock strings too; do not reinterpret their timezone.
    for row in state.conn.execute("SELECT * FROM reservations WHERE room_id=? AND event_date=? AND status='confirmed'", (room_id, data["date"])):
        if row["request_id"] == exclude_request:
            continue
        try:
            start_hour, start_minute = map(int, row["start_time"].split(":"))
            end_hour, end_minute = map(int, row["end_time"].split(":"))
            start = time(start_hour, start_minute)
            end = time(end_hour, end_minute)
        except (TypeError, ValueError):
            return True  # An unreadable occupied slot is not available.
        if start < time.fromisoformat(data["end_time"]) and end > time.fromisoformat(data["start_time"]):
            return True
    return False


def alternatives(state, data, exclude=None):
    normalized, errors = normalize(data)
    if errors or any(key not in normalized for key in ("event_name", "date", "start_time", "end_time", "attendees")):
        return []
    requirements = {key: normalized[key] for key in ("event_name", "date", "start_time", "end_time", "attendees", "required_equipment", "preferred_building") if key in normalized}
    requirements["preferred_room"] = normalized.get("preferred_room") or normalized.get("location")
    result = plan_request(state, requirements)
    return [
        {"id": candidate["room_id"], **{key: candidate[key] for key in ("room_id", "name", "building", "capacity", "equipment", "equipment_match", "preference_matches", "reason_codes", "explanations", "selected_interval")}}
        for candidate in result["feasible_plans"]
        if exclude not in (candidate["room_id"], candidate["name"])
    ]


def operation_binding(req, action, payload):
    actor = payload.get("advisor_id", payload.get("actor", "")) if action == "approve" else payload.get("actor", "student")
    inputs = {key: value for key, value in payload.items() if key not in ("operation_id", "actor", "advisor_id")}
    if action == "execute":
        inputs.setdefault("simulate_failure", False)
    return {"operation_id": payload.get("operation_id"), "actor": actor, "plan_version": req["plan_version"], "input_hash": digest(inputs), "booking_hash": booking_hash(req)}


def prior_operation(state, req, action, binding):
    if not binding["operation_id"]:
        return None, None
    row = state.conn.execute("SELECT * FROM operations WHERE operation_id=?", (binding["operation_id"],)).fetchone()
    if not row:
        return None, None
    # Historical unbound operation keys cannot safely be claimed by a new call.
    if row["request_id"] != req["id"] or row["action"] != action or row["actor"] != binding["actor"] or row["input_hash"] != binding["input_hash"]:
        return row, error(409, "Operation key is already bound to a different workflow, action, actor, or input.", req)
    if row["phase"] == "done":
        return row, (row["status"], json.loads(row["response"]))
    if row["plan_version"] != req["plan_version"] or row["booking_hash"] != booking_hash(req):
        return row, error(409, "Unresolved operation no longer matches the approved plan.", req)
    return row, None


def store_operation(state, req, action, binding, result, phase="done"):
    if not binding["operation_id"]:
        return
    state.conn.execute("INSERT INTO operations (operation_id,request_id,action,status,response,actor,plan_version,input_hash,booking_hash,phase) VALUES (?,?,?,?,?,?,?,?,?,?)", (binding["operation_id"], req["id"], action, result[0], canonical(result[1]), binding["actor"], binding["plan_version"], binding["input_hash"], binding["booking_hash"], phase))


def edit_plan(state, req, action, payload):
    if req["status"] == "completed":
        return error(409, "Completed workflows cannot be reopened.", req)
    if req["status"] == "verifying" or state.conn.execute("SELECT 1 FROM reservations WHERE request_id=?", (req["id"],)).fetchone():
        return error(409, "Reconcile the existing reservation before changing this workflow.", req)
    data, errors = normalize({**req["data"], **{key: value for key, value in payload.items() if key in FIELDS}})
    missing = [key for key in REQUIRED if key not in data]
    if errors:
        return error(422, "Invalid event details.", req, validation_errors=errors)
    changed = data != req["data"]
    if action == "plan" and not changed and req["plan_version"] > 0:
        return 200, public(req) | {"planning": planning_result(state, req)}
    if changed or req["plan_version"] == 0:
        req["data"] = data
        req["plan_version"] += 1
        if req["approval"]:
            record(req, "approval_invalidated", "orchestrator", "Material booking change requires renewed advisor approval")
        req["approval"] = None
        req["steps"] = steps(data)
        req.pop("recovery", None)
    req["missing_fields"] = missing
    if action == "submit":
        if missing or not assigned_advisor(state, data):
            req["status"] = "needs_information"
            save(state, req)
            return error(422, "Complete valid event details and an assigned advisor before submission.", req, validation_errors=[f"{key} is required" for key in missing] or ["Assigned advisor is unavailable"])
        if not changed and req["status"] in {"approved", "pending_approval", "failed"}:
            return 200, public(req)
        req["status"] = "pending_approval"
        record(req, "submitted_for_approval", "student")
    else:
        req["status"] = "needs_information" if missing else "planned"
        record(req, "plan_created", "orchestrator", "Missing: " + (", ".join(missing) or "none"))
    save(state, req)
    return 200, public(req) | {"planning": planning_result(state, req)}


def approve(state, req, payload):
    if "plan_version" in payload and payload["plan_version"] != req["plan_version"]:
        return error(409, "Approval requires the current plan version.", req)
    if req["status"] not in {"pending_approval", "approved"}:
        return error(409, "Request must be submitted before approval.", req)
    data, errors = normalize(req["data"])
    missing = [key for key in REQUIRED if key not in data]
    if errors or missing or data != req["data"]:
        return error(422, "Revalidate and submit a complete canonical plan before approval.", req, validation_errors=errors + [f"{key} is required" for key in missing])
    advisor = assigned_advisor(state, data)
    actor = payload.get("advisor_id", payload.get("actor"))
    if not advisor or actor != advisor["id"] or payload.get("role", "advisor") != "advisor":
        return error(403, "Only the assigned active advisor can approve this request.", req)
    if req["status"] == "approved":
        if req["approval"] and req["approval"].get("plan_hash") == booking_hash(req) and req["approval"].get("plan_version") == req["plan_version"]:
            return 200, public(req)
        return error(409, "Existing approval does not match the current plan.", req)
    req["approval"] = {"actor": advisor["id"], "name": advisor["name"], "at": now(), "plan_version": req["plan_version"], "plan_hash": booking_hash(req)}
    req["status"] = "approved"
    for step in req["steps"]:
        if step["id"] == "advisor":
            step["status"] = "complete"
    record(req, "advisor_approved", actor)
    save(state, req)
    return 200, public(req)


def valid_approval(state, req):
    approval = req["approval"]
    advisor = assigned_advisor(state, req["data"])
    return bool(approval and advisor and approval.get("actor") == advisor["id"] and approval.get("plan_version") == req["plan_version"] and approval.get("plan_hash") == booking_hash(req))


def selected_candidate(state, data):
    requirements = planning_requirements(data)
    room = room_for(state, data["location"])
    if room is None:
        requirements.pop("preferred_room", None)
    result = plan_request(state, requirements)
    if result["validation_errors"]:
        return result, None
    candidate = next((item for item in result["candidates"] if item["room_id"] == (room["id"] if room else None)), None)
    return result, candidate


def fault(state, phase):
    if state.fault_hook is not None:
        state.fault_hook(phase)


def prepare_execution(state, req, payload, binding, prior):
    """Called while holding BEGIN IMMEDIATE; returns (result, needs_readback, created)."""
    if req["status"] == "completed":
        return error(409, "Completed workflows cannot be executed again.", req), False, False
    if "plan_version" in payload and payload["plan_version"] != req["plan_version"]:
        return error(409, "Execution requires the current plan version.", req), False, False
    if req["status"] not in {"approved", "failed", "verifying"} or not valid_approval(state, req):
        return error(409, "Approval of the current canonical plan is required before execution.", req), False, False
    data, errors = normalize(req["data"])
    if errors or data != req["data"] or any(key not in data for key in REQUIRED):
        return error(422, "Cannot execute invalid or legacy event details; revalidate the plan.", req, validation_errors=errors), False, False
    existing = state.conn.execute("SELECT * FROM reservations WHERE request_id=?", (req["id"],)).fetchone()
    if existing:
        # Reconcile before checking availability or overlaps; never overlap ourselves.
        req["status"] = "verifying"
        if not req.get("reservation_id"):
            req["reservation_id"] = existing["id"]
        save(state, req)
        return (202, public(req)), True, False
    if req["status"] == "verifying" or prior is not None:
        return error(502, "Pending reservation is missing from the ledger; reconciliation is required.", req), True, False
    if payload.get("simulate_failure", False):
        req["status"] = "failed"
        req["recovery"] = "Synthetic pre-write failure: retry execution with a new operation key."
        record(req, "tool_failed", "reservation-tool", "Synthetic service unavailable before any reservation write")
        save(state, req)
        return error(502, "Synthetic reservation service failed.", req), False, False
    planning, candidate = selected_candidate(state, data)
    if planning["validation_errors"]:
        return error(422, "Cannot execute invalid planning requirements.", req, validation_errors=planning["validation_errors"]), False, False
    room = room_for(state, data["location"])
    if not room or not candidate or not candidate["feasibility"]:
        conflict = any(code == "confirmed_reservation_conflict" for code in (candidate or {}).get("hard_constraints_failed", []))
        message = "Requested room overlaps an existing reservation." if conflict else "Requested room is unavailable or does not meet the required constraints."
        return error(409, message, req, alternatives=alternatives(state, data, room["id"] if room else None), planning=planning), False, False
    if candidate["selected_interval"]["start_time"] != data["start_time"] or candidate["selected_interval"]["end_time"] != data["end_time"]:
        return error(409, "The available flexible interval requires a revised plan before execution.", req, alternatives=alternatives(state, data, room["id"]), planning=planning), False, False
    reservation_id = "res-" + uuid.uuid4().hex[:10]
    state.conn.execute("INSERT INTO reservations (id,request_id,room_id,event_name,event_date,start_time,end_time,attendees,status,operation_id,created_at,start_at,end_at,timezone,plan_hash,plan_version,booking_snapshot) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (reservation_id, req["id"], room["id"], data["event_name"], data["date"], data["start_time"], data["end_time"], data["attendees"], "confirmed", binding["operation_id"], now(), data["start_at"], data["end_at"], data["timezone"], booking_hash(req), req["plan_version"], canonical(data)))
    req["status"] = "verifying"
    req["reservation_id"] = reservation_id
    req["recovery"] = "Reservation committed; independent read-back verification is pending."
    record(req, "reservation_recorded", "reservation-tool", reservation_id)
    save(state, req)
    return (202, public(req)), True, True


def verify_execution(state, rid):
    fault(state, "before_verification")
    # A separate transaction reads the committed ledger, not insertion variables.
    with transaction(state):
        req = load(state, rid)
        if req is None:
            return error(404, "Request not found")
        stored = state.conn.execute("SELECT * FROM reservations WHERE request_id=?", (rid,)).fetchone()
        data = req["data"]
        room = room_for(state, data.get("location", ""))
        expected = {"id": req.get("reservation_id"), "request_id": rid, "room_id": room["id"] if room else None, "event_name": data.get("event_name"), "event_date": data.get("date"), "start_time": data.get("start_time"), "end_time": data.get("end_time"), "attendees": data.get("attendees"), "status": "confirmed", "start_at": data.get("start_at"), "end_at": data.get("end_at"), "timezone": data.get("timezone"), "plan_hash": booking_hash(req), "plan_version": req["plan_version"], "booking_snapshot": canonical(data)}
        original = state.conn.execute("SELECT * FROM operations WHERE operation_id=?", (stored["operation_id"],)).fetchone() if stored else None
        verified = bool(stored and room and valid_approval(state, req) and all(stored[key] == value for key, value in expected.items()) and original and original["request_id"] == rid and original["action"] == "execute" and original["booking_hash"] == booking_hash(req) and original["plan_version"] == req["plan_version"])
        if not verified or stored is None or room is None:
            req["status"] = "verifying"
            req["recovery"] = "Ledger read-back mismatch; reconcile the existing reservation before retrying."
            save(state, req)
            result = error(502, "Reservation ledger verification failed.", req)
            state.conn.execute("UPDATE operations SET status=?,response=? WHERE request_id=? AND action='execute' AND phase='verifying'", (result[0], canonical(result[1]), rid))
            return result
        if req["status"] != "completed":
            operation_id = stored["operation_id"]
            req["status"] = "completed"
            req.pop("recovery", None)
            req["evidence"] = [{"type": "tool_result", "reference": stored["id"], "room": room["name"], "result": "Reservation recorded in synthetic campus system"}, {"type": "read_back", "reservation_id": stored["id"], "operation_id": operation_id, "verified": True, "plan_hash": booking_hash(req)}, {"type": "completion", "request_id": rid, "reservation_id": stored["id"], "room": room["name"], "date": data["date"], "start_time": data["start_time"], "end_time": data["end_time"], "start_at": data["start_at"], "end_at": data["end_at"], "timezone": data["timezone"], "advisor_approval": req["approval"], "operation_id": operation_id, "plan_hash": booking_hash(req), "verified": True, "verification_result": "verified against the local synthetic reservation ledger", "verification_timestamp": now()}]
            for step in req["steps"]:
                step["status"] = "complete"
            record(req, "workflow_completed", "verification", "All material ledger fields and canonical plan hash verified")
            save(state, req)
        result = (200, public(req))
        state.conn.execute("UPDATE operations SET status=?,response=?,phase='done' WHERE request_id=? AND action='execute' AND phase='verifying'", (result[0], canonical(result[1]), rid))
        return result


def handle_request(state, method, path, payload=OMITTED):
    if state is None:
        return error(503, "Application state has not been initialized.")
    if payload is OMITTED:
        payload = {}
    route = urlparse(path).path.rstrip("/") or "/"
    with state.lock:
        try:
            return dispatch(state, method, route, payload)
        except sqlite3.IntegrityError:
            return error(409, "The operation conflicts with an existing ledger entry.")
        except Exception:
            # transaction() has already rolled back. A post-commit interruption
            # intentionally leaves verifying state and its operation recoverable.
            match = re.fullmatch(r"/api/requests/([^/]+)(?:/[^/]+)?", route)
            req = None
            if match:
                try:
                    req = load(state, match[1])
                except Exception:
                    pass
            return error(502, "Operation interrupted; inspect the workflow and retry reconciliation.", req)


def dispatch(state, method, route, payload):
    if method == "GET" and route == "/api/rooms":
        return 200, {"rooms": [{**dict(row), "equipment": json.loads(row["equipment"])} for row in state.conn.execute("SELECT * FROM rooms ORDER BY id")]}
    if method == "GET" and route == "/api/advisors":
        return 200, {"advisors": [dict(row) for row in state.conn.execute("SELECT * FROM advisors WHERE active=1 ORDER BY id")]}
    if method == "POST" and route == "/api/requests":
        payload, errors = schema("create", payload)
        if errors:
            return error(400, "Invalid request body.", validation_errors=errors)
        data, errors = normalize(parse(payload["text"]))
        if errors:
            return error(422, "Invalid event details.", validation_errors=errors)
        rid = "cr-" + uuid.uuid4().hex[:8]
        req = {"id": rid, "request_id": rid, "text": payload["text"], "data": data, "status": "draft", "steps": [], "missing_fields": [], "audit": [], "evidence": [], "approval": None, "plan_version": 0}
        record(req, "request_created", "student", "Natural-language request received")
        with transaction(state):
            state.conn.execute("INSERT INTO requests (id,text,data,status,steps,missing,audit,evidence,approval,plan_version,recovery,reservation_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (rid, req["text"], canonical(data), "draft", "[]", "[]", canonical(req["audit"]), "[]", None, 0, None, None))
        return 201, {**public(req), "request": public(req)}
    match = re.fullmatch(r"/api/requests/([^/]+)(?:/(plan|submit|approve|execute|alternatives))?", route)
    if not match:
        return error(404, "Not found")
    rid, action = match.groups()
    if method == "GET":
        req = load(state, rid)
        if not req:
            return error(404, "Request not found")
        if action is None:
            return 200, req
        if action == "plan":
            return 200, planning_result(state, req)
        if action == "alternatives":
            return 200, {"alternatives": alternatives(state, req["data"], req["data"].get("location"))}
        return error(405, "Method not allowed")
    if method != "POST" or action not in {"plan", "submit", "approve", "execute"}:
        return error(405, "Method not allowed")
    payload, errors = schema(action, payload)
    if errors:
        return error(422, "Invalid request body.", validation_errors=errors)
    needs_readback = created = False
    with transaction(state):
        # The database lock precedes the authoritative read for EVERY mutation.
        req = load(state, rid)
        if not req:
            return error(404, "Request not found")
        binding = operation_binding(req, action, payload)
        if action == "execute" and not binding["operation_id"]:
            # Calls without a caller key retain legacy retry compatibility;
            # explicit keys always reject a different canonical payload.
            binding["operation_id"] = rid + ":reservation:v" + str(req["plan_version"]) + ":" + binding["input_hash"]
        prior, replay = prior_operation(state, req, action, binding)
        if replay is not None:
            return replay
        if action in {"plan", "submit"}:
            result = edit_plan(state, req, action, payload)
        elif action == "approve":
            result = approve(state, req, payload)
        else:
            result, needs_readback, created = prepare_execution(state, req, payload, binding, prior)
        if prior is None:
            store_operation(state, req, action, binding, result, "verifying" if needs_readback else "done")
        if created:
            # Reservation, operation metadata, and verifying workflow are all
            # still uncommitted when this hook runs.
            fault(state, "before_reservation_commit")
    if created:
        fault(state, "after_reservation_commit")
    return verify_execution(state, rid) if needs_readback else result

"""Deterministic, read-only room constraint planning; no runtime AI is used."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date as _date
from datetime import time as _time
from typing import Any

_TIME_RE = re.compile(r"^(?:[01]?\d|2[0-3]):[0-5]\d$")
_EQUIPMENT_ALIASES = {"hybrid camera": "hybrid_camera", "lab tables": "lab_tables"}
_KNOWN_EQUIPMENT = {"projector", "whiteboard", "hybrid_camera", "lab_tables"}
_REQUIRED_FIELDS = ("event_name", "date", "start_time", "end_time", "attendees")
_ALLOWED_FIELDS = {"event_name", "date", "start_time", "end_time", "attendees", "required_equipment", "preferred_room", "preferred_building", "advisor_required", "assigned_advisor", "advisor", "advisor_id", "flexible_time_window"}


def _error(code: str, message: str, field: str | None = None) -> dict[str, str]:
    result = {"code": code, "message": message}
    if field is not None:
        result["field"] = field
    return result


def _equipment_id(value: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[\s-]+", "_", value.strip().lower()))


def _clock(value: Any, field: str, errors: list[dict[str, str]]) -> str | None:
    if not isinstance(value, str) or not _TIME_RE.fullmatch(value.strip()):
        errors.append(_error("invalid_time", f"{field} must use H:MM or HH:MM in 24-hour time", field))
        return None
    hour, minute = map(int, value.strip().split(":"))
    return f"{hour:02d}:{minute:02d}"


def normalize_planning_requirements(requirements: Any) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Return canonical requirements and controlled validation errors."""
    errors: list[dict[str, str]] = []
    if type(requirements) is not dict:
        return {}, [_error("invalid_type", "planning requirements must be an object")]
    for key in sorted(set(requirements) - _ALLOWED_FIELDS):
        errors.append(_error("unknown_field", f"Unknown planning field: {key}", key))
    out: dict[str, Any] = {}
    for field in ("event_name", "preferred_room", "preferred_building"):
        if field in requirements:
            value = requirements[field]
            if not isinstance(value, str) or not value.strip():
                errors.append(_error("invalid_string", f"{field} must be a nonempty string", field))
            else:
                out[field] = value.strip()
    if "date" in requirements:
        value = requirements["date"]
        try:
            out["date"] = _date.fromisoformat(value.strip()).isoformat() if isinstance(value, str) else None
            if out["date"] is None:
                raise ValueError
        except ValueError:
            errors.append(_error("invalid_date", "date must be a valid calendar date in YYYY-MM-DD format", "date"))
    for field in ("start_time", "end_time"):
        if field in requirements:
            value = _clock(requirements[field], field, errors)
            if value is not None:
                out[field] = value
    if "start_time" in out and "end_time" in out and out["start_time"] >= out["end_time"]:
        errors.append(_error("invalid_interval", "end_time must be after start_time on the same date"))
    if "attendees" in requirements:
        value = requirements["attendees"]
        if type(value) is not int or value <= 0:
            errors.append(_error("invalid_attendees", "attendees must be a positive integer", "attendees"))
        else:
            out["attendees"] = value
    if "required_equipment" in requirements:
        value = requirements["required_equipment"]
        if type(value) is not list or any(not isinstance(item, str) or not item.strip() for item in value):
            errors.append(_error("invalid_equipment", "required_equipment must be a list of nonempty strings", "required_equipment"))
        else:
            equipment = []
            for item in value:
                normalized = _EQUIPMENT_ALIASES.get(item.strip().lower(), _equipment_id(item))
                if normalized not in _KNOWN_EQUIPMENT:
                    errors.append(_error("unknown_equipment", f"Unknown equipment: {item.strip()}", "required_equipment"))
                elif normalized not in equipment:
                    equipment.append(normalized)
            out["required_equipment"] = equipment
    else:
        out["required_equipment"] = []
    if "advisor_required" in requirements:
        if type(requirements["advisor_required"]) is not bool:
            errors.append(_error("invalid_type", "advisor_required must be a boolean", "advisor_required"))
        else:
            out["advisor_required"] = requirements["advisor_required"]
    else:
        out["advisor_required"] = False
    advisor_keys = [key for key in ("assigned_advisor", "advisor", "advisor_id") if key in requirements]
    if advisor_keys:
        values = {str(requirements[key]).strip().lower() for key in advisor_keys}
        if len(values) > 1:
            errors.append(_error("conflicting_advisor", "assigned_advisor, advisor, and advisor_id must agree"))
        value = requirements[advisor_keys[0]]
        if not isinstance(value, str) or not value.strip():
            errors.append(_error("invalid_string", "assigned_advisor must be a nonempty string", "assigned_advisor"))
        else:
            out["assigned_advisor"] = value.strip()
    else:
        out["assigned_advisor"] = None
    if out["advisor_required"] and not out["assigned_advisor"]:
        errors.append(_error("missing_advisor", "assigned_advisor is required when advisor_required is true", "assigned_advisor"))
    if "flexible_time_window" in requirements:
        window = requirements["flexible_time_window"]
        if type(window) is not dict:
            errors.append(_error("invalid_time_window", "flexible_time_window must be an object", "flexible_time_window"))
        else:
            start = _clock(window.get("start_time"), "flexible_time_window.start_time", errors)
            end = _clock(window.get("end_time"), "flexible_time_window.end_time", errors)
            if start and end and start >= end:
                errors.append(_error("invalid_interval", "flexible time window end_time must be after start_time", "flexible_time_window"))
            out["flexible_time_window"] = {"start_time": start, "end_time": end}
    return out, errors


def _conn(state: Any) -> sqlite3.Connection:
    connection = getattr(state, "conn", state)
    if not isinstance(connection, sqlite3.Connection):
        raise TypeError("state must be a sqlite3 connection or an object with .conn")
    return connection


def _get(row: Any, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def _room_equipment(raw: Any) -> list[str]:
    try:
        values = json.loads(raw or "[]") if isinstance(raw, str) else (raw or [])
    except (TypeError, ValueError):
        values = []
    return sorted({_EQUIPMENT_ALIASES.get(str(value).strip().lower(), _equipment_id(str(value))) for value in values if str(value).strip()})


def _conflicts(connection: sqlite3.Connection, room_id: str, day: str, start: _time, end: _time) -> list[dict[str, Any]]:
    rows = connection.execute("SELECT id, start_time, end_time FROM reservations WHERE room_id=? AND event_date=? AND status='confirmed' ORDER BY start_time, end_time, id", (room_id, day)).fetchall()
    conflicts = []
    for row in rows:
        item = {"reservation_id": _get(row, "id"), "start_time": _get(row, "start_time"), "end_time": _get(row, "end_time")}
        try:
            overlaps = _time.fromisoformat(str(item["start_time"])) < end and _time.fromisoformat(str(item["end_time"])) > start
        except (TypeError, ValueError):
            overlaps = True
        if overlaps:
            conflicts.append(item)
    return conflicts


def plan_request(state: Any, requirements: Any) -> dict[str, Any]:
    """Plan against inventory and confirmed reservations using reads only."""
    normalized, errors = normalize_planning_requirements(requirements)
    supplied = requirements if isinstance(requirements, dict) else {}
    for field in _REQUIRED_FIELDS:
        if field not in supplied:
            errors.append(_error("missing_field", f"{field} is required for planning", field))
    result = {"interpreted_goal": "Find an available room satisfying all hard constraints and rank it by stated preferences.", "requirements": normalized, "preferences": {key: normalized.get(key) for key in ("preferred_room", "preferred_building") if normalized.get(key) is not None}, "facts_consulted": ["rooms inventory", "confirmed reservations"], "ranking_policy": ["preferred room", "preferred building", "requested time before flexible time", "minimum capacity surplus", "room_id ascending"], "candidates": [], "feasible_plans": [], "recommended_plan": None, "clarification_required": [], "validation_errors": errors, "no_feasible_plan": False}
    if errors:
        result["clarification_required"] = ["Provide valid event_name, date, start_time, end_time, and attendees."] if any(e["code"] == "missing_field" for e in errors) else []
        result["no_feasible_plan"] = True
        return result
    connection = _conn(state)
    rooms = connection.execute("SELECT id, name, building, capacity, available, equipment FROM rooms ORDER BY id").fetchall()
    preferred = normalized.get("preferred_room")
    def matches_preference(room: Any, value: str) -> bool:
        target = value.strip().lower()
        room_id = str(_get(room, "id")).lower()
        name = str(_get(room, "name")).lower()
        return target in {room_id, name} or name.endswith(" " + target)

    if preferred and not any(matches_preference(room, preferred) for room in rooms):
        result["validation_errors"].append(_error("unknown_room", f"Unknown preferred room: {preferred}", "preferred_room"))
        result["no_feasible_plan"] = True
        return result
    time_options = [(normalized["start_time"], normalized["end_time"], "requested")]
    if normalized.get("flexible_time_window"):
        window = normalized["flexible_time_window"]
        option = (window["start_time"], window["end_time"], "flexible")
        if option[:2] != time_options[0][:2]:
            time_options.append(option)
    required = set(normalized["required_equipment"])
    for room in rooms:
        room_id, name, building = _get(room, "id"), _get(room, "name"), _get(room, "building")
        equipment = _room_equipment(_get(room, "equipment"))
        missing = sorted(required - set(equipment))
        conflicts_by_option = []
        for option_start, option_end, label in time_options:
            conflicts_by_option.append({"label": label, "start_time": option_start, "end_time": option_end, "conflicts": _conflicts(connection, room_id, normalized["date"], _time.fromisoformat(option_start), _time.fromisoformat(option_end))})
        selected_option = next((option for option in conflicts_by_option if not option["conflicts"]), conflicts_by_option[0])
        conflicts = selected_option["conflicts"]
        preference_matches = []
        if preferred and matches_preference(room, preferred):
            preference_matches.append("preferred_room")
        if normalized.get("preferred_building") and str(building).lower() == normalized["preferred_building"].lower():
            preference_matches.append("preferred_building")
        failed = []
        if not bool(_get(room, "available")): failed.append("room_unavailable")
        if _get(room, "capacity", 0) < normalized["attendees"]: failed.append("insufficient_capacity")
        if missing: failed.append("missing_equipment")
        if all(option["conflicts"] for option in conflicts_by_option): failed.append("confirmed_reservation_conflict")
        feasible = not failed
        candidate = {"room_id": room_id, "name": name, "building": building, "capacity": _get(room, "capacity"), "equipment": equipment, "equipment_match": {"required": sorted(required), "missing": missing}, "availability": {"available": bool(_get(room, "available")), "conflicts": conflicts, "options": conflicts_by_option}, "selected_interval": {"start_time": selected_option["start_time"], "end_time": selected_option["end_time"], "source": selected_option["label"]}, "hard_constraints_satisfied": feasible, "hard_constraints_failed": failed, "preference_matches": preference_matches, "feasibility": feasible, "reason_codes": ["feasible"] if feasible else failed, "explanations": ["All hard constraints are satisfied." if selected_option["label"] == "requested" else "Requested interval is occupied; the explicit flexible interval is available."] if feasible else [value.replace("_", " ") for value in failed], "rank": None, "rank_score": None}
        candidate["_sort"] = (0 if "preferred_room" in preference_matches else 1, 0 if "preferred_building" in preference_matches else 1, 0 if selected_option["label"] == "requested" else 1, max(0, _get(room, "capacity", 0) - normalized["attendees"]), str(room_id))
        result["candidates"].append(candidate)
    result["candidates"].sort(key=lambda item: item["_sort"])
    for rank, candidate in enumerate(result["candidates"], 1):
        candidate["rank"], candidate["rank_score"] = rank, 100000 - rank
        candidate.pop("_sort")
    result["feasible_plans"] = [item for item in result["candidates"] if item["feasibility"]]
    result["recommended_plan"] = result["feasible_plans"][0] if result["feasible_plans"] else None
    result["no_feasible_plan"] = not bool(result["feasible_plans"])
    return result

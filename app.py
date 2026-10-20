"""CampusRelay: deterministic, evidence-first campus event workflow demo."""
from __future__ import annotations

import json
import re
import sys
import threading
import uuid
from copy import deepcopy
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

# Minimum intake fields. Time and advisor are enriched by campus policy defaults
# or a later approval assignment; location is the key student-supplied gate.
REQUIRED = ("event_name", "location", "date", "attendees")


def create_app_state():
    return {"requests": {}, "lock": threading.RLock()}


def _now():
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _record(request, action, actor="system", detail=""):
    request["audit"].append({"at": _now(), "action": action, "actor": actor, "detail": detail})


def _parse(text, extra=None):
    extra = extra or {}
    data = dict(extra)
    for key, pattern in {
        "date": r"\b(20\d{2}-\d{2}-\d{2})\b",
        "start_time": r"\b(\d{1,2}:\d{2})\s*(?:to|[-–])",
        "end_time": r"(?:to|[-–])\s*(\d{1,2}:\d{2})\b",
        "attendees": r"\bfor\s+(\d+)\s+attendees?\b",
    }.items():
        match = re.search(pattern, text, re.I)
        if match:
            data[key] = int(match.group(1)) if key == "attendees" else match.group(1)
    room = re.search(r"(?:book|reserve|in|at)\s+((?:student union|library|engineering|room)[^,.;]*)", text, re.I)
    if room and "location" not in data:
        data["location"] = room.group(1).strip()
    advisor = re.search(r"advisor\s+([A-Z][\w. ]+?)(?:[.;]|$)", text)
    if advisor and "advisor" not in data:
        data["advisor"] = advisor.group(1).strip()
    if "event_name" not in data:
        event = re.search(r"(?:for|about)\s+(?:the\s+)?(.+?)(?:\s+on\s+20\d{2}|\s+at\s+|\s+workshop|\s+event|[.;]|$)", text, re.I)
        if event:
            data["event_name"] = event.group(1).strip().title()
    return data


def _steps(data):
    steps = [
        {"id": "intake", "label": "Understand request", "status": "complete"},
        {"id": "policy", "label": "Check campus policy", "status": "complete"},
        {"id": "room", "label": "Reserve the requested space", "status": "ready"},
        {"id": "advisor", "label": "Obtain advisor approval", "status": "blocked"},
        {"id": "verify", "label": "Verify reservation evidence", "status": "pending"},
    ]
    if data.get("attendees", 0) and int(data["attendees"]) > 50:
        steps.insert(3, {"id": "safety", "label": "Route safety review for large event", "status": "ready"})
    return sorted(steps, key=lambda item: item["id"])


def _public(request):
    return deepcopy(request)


def handle_request(state, method, path, payload=None):
    payload = payload or {}
    route = urlparse(path).path.rstrip("/") or "/"
    with state["lock"]:
        if method == "POST" and route == "/api/requests":
            text = str(payload.get("text", "")).strip()
            if not text:
                return 400, {"error": "Describe the event you need to coordinate."}
            request_id = "cr-" + uuid.uuid4().hex[:8]
            request = {"id": request_id, "text": text, "data": _parse(text), "status": "draft",
                       "steps": [], "missing_fields": [], "audit": [], "evidence": [], "approval": None}
            _record(request, "request_created", "student", "Natural-language request received")
            state["requests"][request_id] = request
            public = _public(request)
            return 201, {**public, "request": public}

        match = re.match(r"^/api/requests/([^/]+)(?:/(plan|submit|approve|execute))?$", route)
        if not match:
            return 404, {"error": "Not found"}
        request = state["requests"].get(match.group(1))
        if not request:
            return 404, {"error": "Request not found"}
        action = match.group(2)
        if method == "GET" and not action:
            return 200, _public(request)
        if method != "POST" or not action:
            return 405, {"error": "Method not allowed"}

        if action == "plan":
            request["data"].update(_parse(request["text"], payload))
            request["missing_fields"] = [field for field in REQUIRED if not request["data"].get(field)]
            request["steps"] = _steps(request["data"])
            request["status"] = "needs_information" if request["missing_fields"] else "planned"
            _record(request, "plan_created", "orchestrator", f"Missing: {', '.join(request['missing_fields']) or 'none'}")
            return 200, _public(request)
        if action == "submit":
            request["data"].update({k: v for k, v in payload.items() if k in REQUIRED})
            request["missing_fields"] = [field for field in REQUIRED if not request["data"].get(field)]
            if request["missing_fields"]:
                request["status"] = "needs_information"
                return 422, _public(request)
            request["status"] = "pending_approval"
            _record(request, "submitted_for_approval", "student")
            return 200, _public(request)
        if action == "approve":
            if request["status"] == "approved":
                return 200, _public(request)
            if payload.get("role") != "advisor":
                return 403, {**_public(request), "error": "Only an authorized advisor can approve this request."}
            request["approval"] = {"actor": payload.get("actor", "advisor"), "at": _now()}
            request["status"] = "approved"
            for step in request["steps"]:
                if step["id"] == "advisor":
                    step["status"] = "complete"
            _record(request, "advisor_approved", request["approval"]["actor"])
            return 200, _public(request)
        if action == "execute":
            if request["status"] not in {"approved", "failed"}:
                return 409, {**_public(request), "error": "Approval is required before execution."}
            if payload.get("simulate_failure"):
                request["status"] = "failed"
                request["recovery"] = "Retry execution after the campus reservation tool is available."
                _record(request, "tool_failed", "reservation-tool", "Synthetic reservation service unavailable")
                return 502, _public(request)
            request["status"] = "completed"
            request["evidence"] = [{"type": "tool_result", "reference": "SYN-ROOM-2026-1105-101", "result": "Reservation recorded in synthetic campus system"},
                                   {"type": "completion", "verified": True, "at": _now()}]
            for step in request["steps"]:
                step["status"] = "complete"
            _record(request, "workflow_completed", "verification", "Reservation reference and completion evidence captured")
            return 200, _public(request)
    return 405, {"error": "Method not allowed"}


class Handler(BaseHTTPRequestHandler):
    state = create_app_state()

    def _send(self, status, body, content_type="application/json"):
        raw = body if isinstance(body, bytes) else (json.dumps(body).encode() if content_type == "application/json" else body.encode())
        self.send_response(status); self.send_header("Content-Type", content_type); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)

    def do_GET(self):
        if self.path in {"/", "/index.html"}:
            self._send(200, (Path(__file__).parent / "index.html").read_bytes(), "text/html; charset=utf-8"); return
        status, body = handle_request(self.state, "GET", self.path); self._send(status, body)

    def do_POST(self):
        try:
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        except json.JSONDecodeError:
            self._send(400, {"error": "Invalid JSON"}); return
        status, body = handle_request(self.state, "POST", self.path, payload); self._send(status, body)

    def log_message(self, format, *args):
        pass


def serve(port=8000):
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"CampusRelay running at http://localhost:{port}")
    server.serve_forever()


if __name__ == "__main__":
    serve(int(sys.argv[1]) if len(sys.argv) > 1 else 8000)

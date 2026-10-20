"""CampusRelay V2: deterministic, evidence-first campus event workflow."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import threading
import uuid
from copy import deepcopy
from datetime import date, datetime, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

REQUIRED = ("event_name", "location", "date", "attendees")
DATE_RE = re.compile(r"^20\d{2}-\d{2}-\d{2}$")
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")
ROOMS = [
    {"id": "room-101", "name": "Student Union room 101", "building": "Student Union", "capacity": 40, "available": 1, "equipment": ["projector", "whiteboard"]},
    {"id": "room-201", "name": "Library room 201", "building": "Library", "capacity": 80, "available": 1, "equipment": ["projector", "hybrid camera"]},
    {"id": "room-301", "name": "Engineering room 301", "building": "Engineering", "capacity": 120, "available": 1, "equipment": ["projector", "lab tables"]},
]
ADVISORS = [
    {"id": "advisor-lee", "name": "Dr. Lee", "role": "advisor", "active": True},
    {"id": "advisor-patel", "name": "Dr. Patel", "role": "advisor", "active": True},
]


def _now():
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _parse(text, extra=None):
    data = dict(extra or {})
    for key, pattern in {"date": r"\b(20\d{2}-\d{2}-\d{2})\b", "start_time": r"\b(\d{1,2}:\d{2})\s*(?:to|[-–])", "end_time": r"(?:to|[-–])\s*(\d{1,2}:\d{2})\b", "attendees": r"\bfor\s+(\d+)\s+attendees?\b"}.items():
        m = re.search(pattern, text, re.I)
        if m and key not in data: data[key] = int(m.group(1)) if key == "attendees" else m.group(1)
    room = re.search(r"((?:student union|library|engineering)\s+room\s+\d+|room\s+\d+)", text, re.I)
    if room and "location" not in data: data["location"] = room.group(1).strip()
    advisor = re.search(r"advisor\s+(.+?)(?:;|$)", text, re.I)
    if advisor and "advisor" not in data: data["advisor"] = advisor.group(1).strip().rstrip(".").strip()
    if "event_name" not in data:
        m = re.search(r"(?:for|about)\s+(?:the\s+)?(.+?)(?:\s+on\s+20\d{2}|\s+at\s+|\s+workshop|\s+event|[.;]|$)", text, re.I)
        if m: data["event_name"] = m.group(1).strip().title()
    return data


def _steps(data):
    steps = [{"id":"intake","label":"Understand request","status":"complete"},{"id":"policy","label":"Check campus policy","status":"complete"},{"id":"room","label":"Reserve the requested space","status":"ready"},{"id":"advisor","label":"Obtain advisor approval","status":"blocked"},{"id":"verify","label":"Verify reservation evidence","status":"pending"}]
    if data.get("attendees", 0) and int(data["attendees"]) > 50: steps.insert(3, {"id":"safety","label":"Route safety review for large event","status":"ready"})
    return sorted(steps, key=lambda x: x["id"])


def _validate(data):
    errors = []
    if data.get("date"):
        if not DATE_RE.match(str(data["date"])): errors.append("date must use YYYY-MM-DD")
        else:
            try: date.fromisoformat(str(data["date"]))
            except ValueError: errors.append("date is not a valid calendar date")
    for key in ("start_time", "end_time"):
        if data.get(key) and not TIME_RE.match(str(data[key])): errors.append(f"{key} must use HH:MM")
    if data.get("start_time") and data.get("end_time") and TIME_RE.match(str(data["start_time"])) and TIME_RE.match(str(data["end_time"])) and str(data["start_time"]) >= str(data["end_time"]): errors.append("end_time must be after start_time")
    try:
        if data.get("attendees") is not None and int(data["attendees"]) <= 0: errors.append("attendees must be a positive integer")
    except (ValueError, TypeError): errors.append("attendees must be a positive integer")
    return errors


class AppState:
    def __init__(self, db_path=None):
        self.db_path = db_path or os.environ.get("CAMPUSRELAY_DB", ":memory:")
        if self.db_path != ":memory:": Path(self.db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._init_db()

    def _init_db(self):
        with self.conn:
            self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS rooms (id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL, building TEXT NOT NULL DEFAULT '', capacity INTEGER NOT NULL, available INTEGER NOT NULL DEFAULT 1, equipment TEXT NOT NULL DEFAULT '[]');
            CREATE TABLE IF NOT EXISTS advisors (id TEXT PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL, active INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, text TEXT NOT NULL, data TEXT NOT NULL, status TEXT NOT NULL, steps TEXT NOT NULL, missing TEXT NOT NULL, audit TEXT NOT NULL, evidence TEXT NOT NULL, approval TEXT, plan_version INTEGER NOT NULL, recovery TEXT, reservation_id TEXT);
            CREATE TABLE IF NOT EXISTS reservations (id TEXT PRIMARY KEY, request_id TEXT NOT NULL, room_id TEXT NOT NULL, event_name TEXT NOT NULL, event_date TEXT NOT NULL, start_time TEXT NOT NULL, end_time TEXT NOT NULL, attendees INTEGER NOT NULL, status TEXT NOT NULL, operation_id TEXT NOT NULL, created_at TEXT NOT NULL, FOREIGN KEY(room_id) REFERENCES rooms(id));
            CREATE UNIQUE INDEX IF NOT EXISTS reservations_request_idx ON reservations(request_id);
            CREATE UNIQUE INDEX IF NOT EXISTS reservations_operation_idx ON reservations(operation_id);
            CREATE TABLE IF NOT EXISTS operations (operation_id TEXT PRIMARY KEY, request_id TEXT NOT NULL, action TEXT NOT NULL, status INTEGER NOT NULL, response TEXT NOT NULL);
            """)
            for room in ROOMS: self.conn.execute("INSERT OR IGNORE INTO rooms VALUES (?,?,?,?,?,?)", (room["id"], room["name"], room["building"], room["capacity"], room["available"], json.dumps(room["equipment"])))
            for advisor in ADVISORS: self.conn.execute("INSERT OR IGNORE INTO advisors VALUES (?,?,?,?)", (advisor["id"], advisor["name"], advisor["role"], int(advisor["active"])))

    def close(self): self.conn.close()


def create_app_state(db_path=None):
    return AppState(db_path)


def _public(row):
    if isinstance(row, dict): return deepcopy(row)
    data = json.loads(row["data"]); rid = row["id"]
    result = {"id": rid, "request_id": rid, "text": row["text"], "data": data, "status": row["status"], "steps": json.loads(row["steps"]), "missing_fields": json.loads(row["missing"]), "audit": json.loads(row["audit"]), "evidence": json.loads(row["evidence"]), "approval": json.loads(row["approval"]) if row["approval"] else None, "plan_version": row["plan_version"]}
    if row["recovery"]: result["recovery"] = row["recovery"]
    if row["reservation_id"]: result["reservation_id"] = row["reservation_id"]
    return result


def _save(state, request):
    state.conn.execute("UPDATE requests SET data=?,status=?,steps=?,missing=?,audit=?,evidence=?,approval=?,plan_version=?,recovery=?,reservation_id=? WHERE id=?", (json.dumps(request["data"]), request["status"], json.dumps(request["steps"]), json.dumps(request["missing_fields"]), json.dumps(request["audit"]), json.dumps(request["evidence"]), json.dumps(request["approval"]) if request["approval"] else None, request["plan_version"], request.get("recovery"), request.get("reservation_id"), request["id"]))


def _record(request, action, actor="system", detail=""):
    request["audit"].append({"at": _now(), "action": action, "actor": actor, "detail": detail})


def _error(status, message, request=None, **extra):
    body = dict(extra); body["error"] = message
    if request: body.update(_public(request))
    return status, body


def _operation(state, request_id, action, payload):
    op = payload.get("operation_id") or payload.get("operationId")
    if not op: return None
    row = state.conn.execute("SELECT status,response FROM operations WHERE operation_id=?", (str(op),)).fetchone()
    if row: return int(row["status"]), json.loads(row["response"])
    return (str(op),)


def _store_operation(state, op, request_id, action, result):
    if isinstance(op, tuple):
        state.conn.execute("INSERT OR IGNORE INTO operations VALUES (?,?,?,?,?)", (op[0], request_id, action, result[0], json.dumps(result[1])))


def _alternatives(state, data, exclude=None):
    if not data.get("date") or not data.get("start_time") or not data.get("end_time"): return []
    cap = int(data.get("attendees", 0) or 0); rows = state.conn.execute("SELECT * FROM rooms WHERE capacity>=? AND available=1 ORDER BY building, capacity, id", (cap,)).fetchall(); out=[]
    for room in rows:
        if exclude and room["id"] == exclude: continue
        conflict = state.conn.execute("SELECT 1 FROM reservations WHERE room_id=? AND event_date=? AND status='confirmed' AND start_time<? AND end_time>?", (room["id"], data["date"], data["end_time"], data["start_time"])).fetchone()
        if not conflict: out.append({"id":room["id"],"name":room["name"],"building":room["building"],"capacity":room["capacity"],"equipment":json.loads(room["equipment"])})
    return out


def handle_request(state, method, path, payload=None):
    payload = payload or {}; route = urlparse(path).path.rstrip("/") or "/"
    with state.lock:
        if method == "GET" and route == "/api/rooms":
            rooms=[]
            for r in state.conn.execute("SELECT id,name,building,capacity,available,equipment FROM rooms ORDER BY id"):
                rooms.append({**dict(r), "equipment": json.loads(r["equipment"])})
            return 200, {"rooms":rooms}
        if method == "GET" and route == "/api/advisors": return 200, {"advisors":[dict(r) for r in state.conn.execute("SELECT id,name,role,active FROM advisors WHERE active=1 ORDER BY id")]}
        if method == "POST" and route == "/api/requests":
            text = str(payload.get("text", "")).strip()
            if not text: return _error(400, "Describe the event you need to coordinate.")
            rid = "cr-" + uuid.uuid4().hex[:8]; data = _parse(text); req={"id":rid,"text":text,"data":data,"status":"draft","steps":[],"missing_fields":[],"audit":[],"evidence":[],"approval":None,"plan_version":0}
            _record(req,"request_created","student","Natural-language request received")
            state.conn.execute("INSERT INTO requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (rid,text,json.dumps(data),"draft","[]","[]",json.dumps(req["audit"]),"[]",None,0,None,None)); public=_public(state.conn.execute("SELECT * FROM requests WHERE id=?",(rid,)).fetchone()); return 201,{**public,"request":public}
        match = re.match(r"^/api/requests/([^/]+)(?:/(plan|submit|approve|execute|alternatives))?$", route)
        if not match: return _error(404,"Not found")
        row = state.conn.execute("SELECT * FROM requests WHERE id=?", (match.group(1),)).fetchone()
        if not row: return _error(404,"Request not found")
        req = _public(row); action = match.group(2)
        if method == "GET" and not action: return 200, req
        if method == "GET" and action == "alternatives": return 200, {"alternatives":_alternatives(state, req["data"], req["data"].get("location"))}
        if method != "POST" or not action: return _error(405,"Method not allowed")
        op = _operation(state, req["id"], action, payload)
        if op and len(op)==2: return op
        if action == "plan":
            if req["status"] == "completed": return _error(409,"Completed workflows cannot be replanned.",req)
            before = dict(req["data"])
            req["data"].update(_parse(req["text"], payload)); errors=_validate(req["data"]); req["missing_fields"]=[f for f in REQUIRED if not req["data"].get(f)]
            if errors: return _error(422,"Invalid event details: "+"; ".join(errors),req,validation_errors=errors)
            changed = before != req["data"]
            req["steps"]=_steps(req["data"]); req["status"]="needs_information" if req["missing_fields"] else "planned"; req["plan_version"] += 1
            if req["approval"] and changed:
                req["approval"] = None
                _record(req,"approval_invalidated","orchestrator","Material booking change requires renewed advisor approval")
            _record(req,"plan_created","orchestrator",f"Missing: {', '.join(req['missing_fields']) or 'none'}"); _save(state,req); result=(200,_public(req)); _store_operation(state,op,req["id"],action,result); return result
        if action == "submit":
            req["data"].update({k:v for k,v in payload.items() if k in REQUIRED or k in ("start_time","end_time","location","advisor")}); errors=_validate(req["data"]); req["missing_fields"]=[f for f in REQUIRED if not req["data"].get(f)]
            if errors or req["missing_fields"]: req["status"]="needs_information"; _save(state,req); return _error(422,"Complete valid event details before submission.",req,validation_errors=errors)
            req["status"]="pending_approval"; _record(req,"submitted_for_approval","student"); _save(state,req); result=(200,_public(req)); _store_operation(state,op,req["id"],action,result); return result
        if action == "approve":
            if req["status"] == "approved": return 200, req
            if req["status"] != "pending_approval": return _error(409,"Request must be submitted before approval.",req)
            advisor_id=str(payload.get("advisor_id") or payload.get("actor") or ""); advisor=state.conn.execute("SELECT * FROM advisors WHERE id=? AND active=1",(advisor_id,)).fetchone()
            if payload.get("role") != "advisor" or not advisor: return _error(403,"Only an authorized advisor can approve this request.",req)
            if payload.get("plan_version") is not None and int(payload["plan_version"]) != req["plan_version"]: return _error(409,"Approval requires the current plan version.",req)
            req["approval"]={"actor":advisor_id,"name":advisor["name"],"at":_now(),"plan_version":req["plan_version"]}; req["status"]="approved"
            for step in req["steps"]:
                if step["id"]=="advisor": step["status"]="complete"
            _record(req,"advisor_approved",advisor_id); _save(state,req); result=(200,_public(req)); _store_operation(state,op,req["id"],action,result); return result
        if action == "execute":
            if req["status"] == "completed": return _error(409,"Completed workflows cannot be executed again.",req)
            if req["status"] not in {"approved","failed"} or not req["approval"] or req["approval"].get("plan_version") != req["plan_version"]: return _error(409,"Approval is required before execution.",req)
            if payload.get("simulate_failure"): req["status"]="failed"; req["recovery"]="Retry execution after the campus reservation tool is available."; _record(req,"tool_failed","reservation-tool","Synthetic reservation service unavailable"); _save(state,req); result=_error(502,"Synthetic reservation service failed.",req); _store_operation(state,op,req["id"],action,result); return result
            data=req["data"]; errors=_validate(data)
            if errors: return _error(422,"Cannot execute invalid event details.",req,validation_errors=errors)
            location = str(data.get("location", "")).strip()
            room=state.conn.execute("SELECT * FROM rooms WHERE lower(name)=lower(?) OR lower(name) LIKE lower(?)",(location,"%"+location+"%" )).fetchone()
            if not room or not room["available"] or room["capacity"] < int(data["attendees"]): return _error(409,"Requested room is unavailable or too small.",req,alternatives=_alternatives(state,data))
            try:
                state.conn.execute("BEGIN IMMEDIATE")
                conflict=state.conn.execute("SELECT 1 FROM reservations WHERE room_id=? AND event_date=? AND status='confirmed' AND start_time<? AND end_time>?",(room["id"],data["date"],data["end_time"],data["start_time"])).fetchone()
                if conflict: state.conn.execute("ROLLBACK"); return _error(409,"Requested room overlaps an existing reservation.",req,alternatives=_alternatives(state,data,room["id"]))
                operation_id = str(payload.get("operation_id") or payload.get("operationId") or (req["id"] + ":reservation:v" + str(req["plan_version"])))
                existing = state.conn.execute("SELECT * FROM reservations WHERE operation_id=?", (operation_id,)).fetchone()
                if existing:
                    state.conn.execute("COMMIT")
                    return 200, _public(req) | {"reservation_id": existing["id"], "operation_id": operation_id, "idempotent_replay": True}
                reservation_id="res-"+uuid.uuid4().hex[:10]
                state.conn.execute("INSERT INTO reservations VALUES (?,?,?,?,?,?,?,?,?,?,?)",(reservation_id,req["id"],room["id"],data.get("event_name", "Campus event"),data["date"],data["start_time"],data["end_time"],int(data["attendees"]),"confirmed",operation_id,_now()))
                state.conn.execute("COMMIT")
            except sqlite3.IntegrityError: state.conn.execute("ROLLBACK"); return _error(409,"Request already has a reservation.",req)
            stored = state.conn.execute("SELECT * FROM reservations WHERE id=?", (reservation_id,)).fetchone()
            verified = bool(stored and stored["request_id"] == req["id"] and stored["room_id"] == room["id"] and stored["event_date"] == data["date"] and stored["start_time"] == data["start_time"] and stored["end_time"] == data["end_time"] and stored["status"] == "confirmed")
            if not verified: return _error(502,"Reservation created but ledger verification failed.",req)
            operation_id = stored["operation_id"]
            req["status"]="completed"; req["reservation_id"]=reservation_id; req["evidence"]=[{"type":"tool_result","reference":reservation_id,"room":room["name"],"result":"Reservation recorded in synthetic campus system"},{"type":"read_back","reservation_id":reservation_id,"operation_id":operation_id,"verified":True},{"type":"completion","request_id":req["id"],"reservation_id":reservation_id,"room":room["name"],"date":data["date"],"start_time":data["start_time"],"end_time":data["end_time"],"advisor_approval":req["approval"],"operation_id":operation_id,"verified":True,"verification_result":"verified against the local synthetic reservation ledger","verification_timestamp":_now()}]
            for step in req["steps"]: step["status"]="complete"
            _record(req,"workflow_completed","verification","Reservation reference and read-back evidence captured"); _save(state,req); result=(200,_public(req)); _store_operation(state,op,req["id"],action,result); return result
    return _error(405,"Method not allowed")


class Handler(BaseHTTPRequestHandler):
    state = create_app_state()
    def _send(self,status,body,content_type="application/json"):
        raw=body if isinstance(body,bytes) else (json.dumps(body).encode() if content_type.startswith("application/json") else body.encode()); self.send_response(status); self.send_header("Content-Type",content_type); self.send_header("Content-Length",str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def do_GET(self):
        if self.path in {"/","/index.html"}: self._send(200,(Path(__file__).parent/"index.html").read_bytes(),"text/html; charset=utf-8"); return
        status,body=handle_request(self.state,"GET",self.path); self._send(status,body)
    def do_POST(self):
        try: payload=json.loads(self.rfile.read(int(self.headers.get("Content-Length",0))) or b"{}")
        except json.JSONDecodeError: self._send(400,{"error":"Invalid JSON"}); return
        status,body=handle_request(self.state,"POST",self.path,payload); self._send(status,body)
    def log_message(self,format,*args): pass


def serve(port=8000):
    server=ThreadingHTTPServer(("0.0.0.0",port),Handler); print(f"CampusRelay running at http://localhost:{port}"); server.serve_forever()

if __name__ == "__main__": serve(int(sys.argv[1]) if len(sys.argv)>1 else 8000)

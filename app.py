"""CampusRelay V2: deterministic, evidence-first campus event workflow."""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from backend_integrity import OMITTED, handle_request as integrity_request, migrate, normalize
ROOMS = [
    {"id": "room-101", "name": "Student Union room 101", "building": "Student Union", "capacity": 40, "available": 1, "equipment": ["projector", "whiteboard"]},
    {"id": "room-201", "name": "Library room 201", "building": "Library", "capacity": 80, "available": 1, "equipment": ["projector", "hybrid camera"]},
    {"id": "room-301", "name": "Engineering room 301", "building": "Engineering", "capacity": 120, "available": 1, "equipment": ["projector", "lab tables"]},
]
ADVISORS = [
    {"id": "advisor-lee", "name": "Dr. Lee", "role": "advisor", "active": True},
    {"id": "advisor-patel", "name": "Dr. Patel", "role": "advisor", "active": True},
]


def _validate(data):
    if type(data) is not dict: return ["Event details must be an object"]
    return normalize(data)[1]


class AppState:
    def __init__(self, db_path=None):
        self.db_path = db_path or os.environ.get("CAMPUSRELAY_DB", ":memory:")
        if self.db_path != ":memory:": Path(self.db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.fault_hook: Callable[[str], None] | None = None
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")
        try:
            self._init_db()
            migrate(self)
        except BaseException:
            if self.conn.in_transaction: self.conn.rollback()
            self.conn.close()
            raise

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


def handle_request(state, method, path, payload=OMITTED):
    return integrity_request(state, method, path, payload)


class Handler(BaseHTTPRequestHandler):
    state: AppState | None = None
    def _send(self,status,body,content_type="application/json"):
        raw=body if isinstance(body,bytes) else (json.dumps(body).encode() if content_type.startswith("application/json") else body.encode()); self.send_response(status); self.send_header("Content-Type",content_type); self.send_header("Content-Length",str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def do_GET(self):
        if self.path in {"/","/index.html"}: self._send(200,(Path(__file__).parent/"index.html").read_bytes(),"text/html; charset=utf-8"); return
        status,body=handle_request(self.state,"GET",self.path); self._send(status,body)
    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length < 0 or length > 1048576: raise ValueError("Invalid body length")
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError): self._send(400,{"error":"Invalid JSON", "error_code":"invalid_request"}); return
        status,body=handle_request(self.state,"POST",self.path,payload); self._send(status,body)
    def log_message(self,format,*args): pass


def serve(port=None):
    port = int(os.environ.get("PORT", "8000")) if port is None else int(port)
    if Handler.state is None: Handler.state = create_app_state()
    server=ThreadingHTTPServer(("0.0.0.0",port),Handler); print(f"CampusRelay running at http://localhost:{port}")
    try: server.serve_forever()
    finally: server.server_close()

if __name__ == "__main__": serve(int(sys.argv[1]) if len(sys.argv)>1 else None)

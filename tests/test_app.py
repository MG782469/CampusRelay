"""Contract tests for the planned CampusRelay application."""

import unittest
import tempfile
import threading
from pathlib import Path

try:
    from app import create_app_state, handle_request
except ModuleNotFoundError as exc:
    if exc.name != "app":
        raise
    create_app_state = handle_request = None
    _APP_AVAILABLE = False
else:
    _APP_AVAILABLE = True


@unittest.skipUnless(_APP_AVAILABLE, "planned app.py is not implemented yet")
class CampusRelayV2Tests(unittest.TestCase):
    EVENT = "Book Student Union room 101 for Robotics workshop on 2026-11-05 from 15:00 to 17:00 for 20 attendees; advisor Dr. Lee."

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = create_app_state(str(Path(self.tmp.name) / "relay.sqlite3"))

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def call(self, method, path, payload=None):
        return handle_request(self.state, method, path, payload)

    def workflow(self, text=None, room=None, start="15:00", end="17:00", attendees=20):
        _, body = self.call("POST", "/api/requests", {"text": text or self.EVENT})
        rid = body["id"]
        self.call("POST", f"/api/requests/{rid}/plan", {})
        payload = {"start_time": start, "end_time": end, "attendees": attendees}
        if room: payload["location"] = room
        self.call("POST", f"/api/requests/{rid}/submit", payload)
        self.call("POST", f"/api/requests/{rid}/approve", {"actor": "advisor-lee", "role": "advisor"})
        return rid

    def test_available_booking_has_unique_ledger_record_and_evidence(self):
        rid = self.workflow()
        status, result = self.call("POST", f"/api/requests/{rid}/execute", {"operation_id": "op-a"})
        self.assertEqual(status, 200)
        self.assertTrue(result["reservation_id"].startswith("res-"))
        completion = next(e for e in result["evidence"] if e["type"] == "completion")
        self.assertEqual(completion["request_id"], rid)
        self.assertIn("local synthetic reservation ledger", completion["verification_result"])

    def test_overlap_rejected_and_alternative_suggested(self):
        first = self.workflow()
        self.assertEqual(self.call("POST", f"/api/requests/{first}/execute", {"operation_id": "op-1"})[0], 200)
        second = self.workflow()
        status, result = self.call("POST", f"/api/requests/{second}/execute", {"operation_id": "op-2"})
        self.assertEqual(status, 409)
        self.assertIn("overlap", result["error"])
        self.assertTrue(any(a["id"] == "room-201" for a in result["alternatives"]))

    def test_adjacent_and_different_room_bookings_are_allowed(self):
        first = self.workflow()
        self.assertEqual(self.call("POST", f"/api/requests/{first}/execute", {"operation_id": "op-adj-1"})[0], 200)
        adjacent = self.workflow(start="17:00", end="18:00")
        self.assertEqual(self.call("POST", f"/api/requests/{adjacent}/execute", {"operation_id": "op-adj-2"})[0], 200)
        other = self.workflow(room="Library room 201")
        self.assertEqual(self.call("POST", f"/api/requests/{other}/execute", {"operation_id": "op-other"})[0], 200)

    def test_capacity_unknown_room_and_invalid_time_are_rejected(self):
        rid = self.workflow(attendees=50)
        self.assertEqual(self.call("POST", f"/api/requests/{rid}/execute", {"operation_id": "op-cap"})[0], 409)
        unknown = self.workflow(room="Room 999")
        self.assertEqual(self.call("POST", f"/api/requests/{unknown}/execute", {"operation_id": "op-room"})[0], 409)
        _, body = self.call("POST", "/api/requests", {"text": self.EVENT})
        rid = body["id"]
        status, result = self.call("POST", f"/api/requests/{rid}/plan", {"date": "2026-02-30", "start_time": "18:00", "end_time": "17:00"})
        self.assertEqual(status, 422)
        self.assertTrue(result["validation_errors"])

    def test_material_plan_change_invalidates_approval(self):
        rid = self.workflow()
        status, changed = self.call("POST", f"/api/requests/{rid}/plan", {"location": "Library room 201"})
        self.assertEqual(status, 200)
        self.assertIsNone(changed["approval"])
        self.assertEqual(self.call("POST", f"/api/requests/{rid}/execute", {"operation_id": "op-change"})[0], 409)

    def test_invalid_transitions_and_duplicate_operation(self):
        _, body = self.call("POST", "/api/requests", {"text": self.EVENT})
        rid = body["id"]
        self.assertEqual(self.call("POST", f"/api/requests/{rid}/approve", {"actor": "advisor-lee", "role": "advisor"})[0], 409)
        self.assertEqual(self.call("POST", f"/api/requests/{rid}/execute", {})[0], 409)
        rid = self.workflow()
        first = self.call("POST", f"/api/requests/{rid}/execute", {"operation_id": "same-op"})
        second = self.call("POST", f"/api/requests/{rid}/execute", {"operation_id": "same-op"})
        self.assertEqual(first[0], second[0])
        self.assertTrue(second[1].get("idempotent_replay") or second[1].get("reservation_id"))
        self.assertEqual(self.call("POST", f"/api/requests/{rid}/execute", {"operation_id": "new-op"})[0], 409)

    def test_parser_keeps_advisor_name_and_restart_persists(self):
        rid = self.workflow()
        current = self.call("GET", f"/api/requests/{rid}")[1]
        self.assertEqual(current["data"]["advisor"], "Dr. Lee")
        db = self.state.db_path
        self.state.close()
        reopened = create_app_state(db)
        try:
            self.assertEqual(handle_request(reopened, "GET", f"/api/requests/{rid}")[1]["id"], rid)
        finally:
            reopened.close()

    def test_concurrent_execution_cannot_double_book(self):
        db = self.state.db_path
        other = create_app_state(db)
        try:
            first = self.workflow()
            _, body = self.call("POST", "/api/requests", {"text": self.EVENT.replace("Robotics", "Design")})
            second = body["id"]
            self.call("POST", f"/api/requests/{second}/plan", {})
            self.call("POST", f"/api/requests/{second}/submit", {"start_time": "15:00", "end_time": "17:00", "attendees": 20})
            self.call("POST", f"/api/requests/{second}/approve", {"actor": "advisor-lee", "role": "advisor"})
            results = []
            def execute(state, request_id, op):
                results.append(handle_request(state, "POST", f"/api/requests/{request_id}/execute", {"operation_id": op})[0])
            a = threading.Thread(target=execute, args=(self.state, first, "concurrent-a"))
            b = threading.Thread(target=execute, args=(other, second, "concurrent-b"))
            a.start(); b.start(); a.join(); b.join()
            self.assertEqual(sorted(results), [200, 409])
        finally:
            other.close()


@unittest.skipUnless(_APP_AVAILABLE, "planned app.py is not implemented yet")
class CampusRelayContractTests(unittest.TestCase):
    """Exercise the end-to-end request lifecycle exposed by ``app.py``."""

    COMPLETE_EVENT = (
        "Book Student Union room 101 for the Robotics Club workshop on "
        "2026-11-05 from 15:00 to 17:00 for 20 attendees; advisor Dr. Lee."
    )

    def setUp(self):
        self.state = create_app_state()

    def request(self, method, path, payload=None, expected_status=None):
        result = handle_request(self.state, method, path, payload)
        self.assertIsInstance(result, tuple, "handle_request must return (status, body)")
        self.assertEqual(len(result), 2)
        status, body = result
        self.assertIsInstance(status, int)
        self.assertIsInstance(body, dict)
        if expected_status is not None:
            self.assertEqual(status, expected_status, body)
        return status, body

    @staticmethod
    def request_id(body):
        request = body.get("request", body)
        request_id = request.get("id") or request.get("request_id")
        if not request_id:
            raise AssertionError(f"response does not contain a request id: {body!r}")
        return request_id

    def create_request(self, text=COMPLETE_EVENT):
        _, body = self.request("POST", "/api/requests", {"text": text}, 201)
        self.assertIn(body.get("status"), {"draft", "needs_information"}, body)
        return self.request_id(body)

    def plan(self, request_id, payload=None, expected_status=200):
        return self.request("POST", f"/api/requests/{request_id}/plan", payload, expected_status)

    def submit(self, request_id, payload=None, expected_status=200):
        return self.request("POST", f"/api/requests/{request_id}/submit", payload, expected_status)

    def approve(self, request_id, payload=None, expected_status=200):
        return self.request("POST", f"/api/requests/{request_id}/approve", payload, expected_status)

    def execute(self, request_id, payload=None, expected_status=200):
        return self.request("POST", f"/api/requests/{request_id}/execute", payload, expected_status)

    def test_happy_path_plans_submits_approves_and_executes(self):
        request_id = self.create_request()
        _, planned = self.plan(request_id)
        self.assertEqual(planned["steps"], sorted(planned["steps"], key=str))
        self.assertEqual(planned.get("missing_fields", []), [])
        _, submitted = self.submit(request_id)
        self.assertIn(submitted["status"], {"pending_approval", "submitted"})
        _, approved = self.approve(request_id, {"actor": "advisor-lee", "role": "advisor"})
        self.assertIn(approved["status"], {"approved", "ready"})
        _, completed = self.execute(request_id)
        self.assertEqual(completed["status"], "completed")

    def test_missing_information_is_flagged_and_blocks_submission(self):
        request_id = self.create_request(
            "Book a room for a Robotics Club workshop on 2026-11-05 for 20 attendees."
        )
        _, planned = self.plan(request_id)
        self.assertTrue(planned["missing_fields"])
        self.assertIn("location", planned["missing_fields"])
        status, rejected = self.submit(request_id, expected_status=422)
        self.assertEqual(status, 422)
        self.assertEqual(rejected["status"], "needs_information")
        self.assertIn("location", rejected["missing_fields"])
        _, supplied = self.submit(request_id, {"location": "Student Union room 101"})
        self.assertNotEqual(supplied["status"], "needs_information")

    def test_approval_requires_an_authorized_advisor(self):
        request_id = self.create_request()
        self.plan(request_id)
        self.submit(request_id)
        status, rejected = self.approve(
            request_id, {"actor": "student-42", "role": "student"}, 403
        )
        self.assertEqual(status, 403)
        self.assertEqual(rejected["status"], "pending_approval")
        self.assertIn("advisor", rejected["error"].lower())

    def test_duplicate_approval_is_idempotent(self):
        request_id = self.create_request()
        self.plan(request_id)
        self.submit(request_id)
        payload = {"actor": "advisor-lee", "role": "advisor"}
        _, first = self.approve(request_id, payload)
        _, current = self.request("GET", f"/api/requests/{request_id}")
        audit_before = list(current.get("audit", []))
        _, second = self.approve(request_id, payload)
        _, current_after = self.request("GET", f"/api/requests/{request_id}")
        self.assertEqual(second["status"], first["status"])
        self.assertEqual(current_after.get("audit", []), audit_before)

    def test_tool_failure_can_be_recovered_by_retrying_execution(self):
        request_id = self.create_request()
        self.plan(request_id)
        self.submit(request_id)
        self.approve(request_id, {"actor": "advisor-lee", "role": "advisor"})
        status, failed = self.execute(request_id, {"simulate_failure": True}, 502)
        self.assertEqual(status, 502)
        self.assertEqual(failed["status"], "failed")
        self.assertTrue(failed.get("recovery"))
        _, recovered = self.execute(request_id, {"simulate_failure": False})
        self.assertEqual(recovered["status"], "completed")

    def test_completed_request_exposes_steps_audit_and_evidence(self):
        request_id = self.create_request()
        self.plan(request_id)
        self.submit(request_id)
        self.approve(request_id, {"actor": "advisor-lee", "role": "advisor"})
        self.execute(request_id)
        status, record = self.request("GET", f"/api/requests/{request_id}")
        self.assertEqual(status, 200)
        self.assertEqual(record["status"], "completed")
        self.assertTrue(record["steps"])
        self.assertTrue(record["audit"])
        self.assertTrue(record["evidence"])
        self.assertTrue(any(item.get("type") in {"tool_result", "completion"} for item in record["evidence"]))


if __name__ == "__main__":
    unittest.main()

import json
import tempfile
import unittest
from pathlib import Path

from app import create_app_state, handle_request


EVENT = "Book Student Union room 101 for Robotics workshop on 2026-11-05 from 15:00 to 17:00 for 20 attendees; advisor Dr. Lee."


class ProcessTerminated(BaseException):
    pass


class Milestone1IntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = create_app_state(str(Path(self.tmp.name) / "milestone1.sqlite3"))

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def call(self, method, path, payload=None):
        return handle_request(self.state, method, path, {} if payload is None else payload)

    def workflow(self, text=EVENT, operation="exec-1"):
        _, created = self.call("POST", "/api/requests", {"text": text})
        rid = created["id"]
        self.assertEqual(self.call("POST", f"/api/requests/{rid}/plan", {})[0], 200)
        self.assertEqual(self.call("POST", f"/api/requests/{rid}/submit", {})[0], 200)
        self.assertEqual(self.call("POST", f"/api/requests/{rid}/approve", {"actor": "advisor-lee", "role": "advisor"})[0], 200)
        return rid, operation

    def test_normalizes_single_digit_hours_and_rejects_reversed_interval(self):
        _, created = self.call("POST", "/api/requests", {"text": "Book Student Union room 101 for Robotics workshop on 2026-11-05 from 9:00 to 10:00 for 20 attendees; advisor Dr. Lee."})
        rid = created["id"]
        status, planned = self.call("POST", f"/api/requests/{rid}/plan", {})
        self.assertEqual(status, 200)
        self.assertEqual(planned["data"]["start_time"], "09:00")
        self.assertEqual(planned["data"]["end_time"], "10:00")
        self.assertEqual(planned["data"]["timezone"], "UTC+05:30")
        self.assertTrue(planned["data"]["start_at"].endswith("+05:30"))
        status, rejected = self.call("POST", f"/api/requests/{rid}/plan", {"start_time": "10:00", "end_time": "09:00"})
        self.assertEqual(status, 422)
        self.assertIn("after", " ".join(rejected["validation_errors"]))

    def test_rejects_invalid_attendance_types_and_missing_execution_fields(self):
        for value in (True, 20.5, "20", 0, -1):
            _, created = self.call("POST", "/api/requests", {"text": EVENT})
            rid = created["id"]
            status, body = self.call("POST", f"/api/requests/{rid}/plan", {"attendees": value})
            self.assertEqual(status, 422, (value, body))
        _, created = self.call("POST", "/api/requests", {"text": "Book Student Union room 101 for Robotics workshop on 2026-11-05 for 20 attendees; advisor Dr. Lee."})
        rid = created["id"]
        self.call("POST", f"/api/requests/{rid}/plan", {})
        status, body = self.call("POST", f"/api/requests/{rid}/submit", {"location": "Student Union room 101"})
        self.assertEqual(status, 422)
        self.assertEqual(body["status"], "needs_information")
        self.assertIn("start_time", body["missing_fields"])
        self.assertIn("end_time", body["missing_fields"])

    def test_noop_plan_preserves_version_and_approval_but_material_change_invalidates(self):
        rid, _ = self.workflow()
        before = self.call("GET", f"/api/requests/{rid}")[1]
        status, same = self.call("POST", f"/api/requests/{rid}/plan", {})
        self.assertEqual(status, 200)
        self.assertEqual(same["plan_version"], before["plan_version"])
        self.assertEqual(same["approval"], before["approval"])
        status, changed = self.call("POST", f"/api/requests/{rid}/plan", {"location": "Library room 201"})
        self.assertEqual(status, 200)
        self.assertEqual(changed["plan_version"], before["plan_version"] + 1)
        self.assertIsNone(changed["approval"])

    def test_completed_workflow_cannot_be_reopened_or_resubmitted(self):
        rid, operation = self.workflow()
        self.assertEqual(self.call("POST", f"/api/requests/{rid}/execute", {"operation_id": operation})[0], 200)
        for action in ("plan", "submit"):
            status, body = self.call("POST", f"/api/requests/{rid}/{action}", {})
            self.assertEqual(status, 409)
            self.assertEqual(body["status"], "completed")

    def test_invalid_approval_version_and_wrong_assigned_advisor_are_rejected(self):
        rid, _ = self.workflow()
        # workflow() is already approved; create a fresh one to inspect approval gate.
        _, created = self.call("POST", "/api/requests", {"text": EVENT})
        fresh = created["id"]
        self.call("POST", f"/api/requests/{fresh}/plan", {})
        self.call("POST", f"/api/requests/{fresh}/submit", {})
        status, body = self.call("POST", f"/api/requests/{fresh}/approve", {"actor": "advisor-patel", "role": "advisor", "plan_version": 1})
        self.assertEqual(status, 403)
        self.assertIn("assigned", body["error"])
        status, body = self.call("POST", f"/api/requests/{fresh}/approve", {"actor": "advisor-lee", "role": "advisor", "plan_version": 99})
        self.assertEqual(status, 409)

    def test_operation_keys_are_bound_to_workflow_action_and_payload(self):
        first, _ = self.workflow(operation="shared-key")
        second, _ = self.workflow(text=EVENT.replace("Robotics", "Design"), operation="other")
        self.assertEqual(self.call("POST", f"/api/requests/{first}/execute", {"operation_id": "shared-key"})[0], 200)
        status, body = self.call("POST", f"/api/requests/{second}/execute", {"operation_id": "shared-key"})
        self.assertEqual(status, 409)
        self.assertIn("already bound", body["error"])
        status, body = self.call("POST", f"/api/requests/{first}/execute", {"operation_id": "shared-key", "plan_version": 99})
        self.assertEqual(status, 409)
        self.assertIn("already bound", body["error"])

    def test_operation_keys_are_bound_across_actions(self):
        _, created = self.call("POST", "/api/requests", {"text": EVENT})
        rid = created["id"]
        self.assertEqual(self.call("POST", f"/api/requests/{rid}/plan", {"operation_id": "action-key"})[0], 200)
        self.assertEqual(self.call("POST", f"/api/requests/{rid}/submit", {})[0], 200)
        status, body = self.call("POST", f"/api/requests/{rid}/approve", {"actor": "advisor-lee", "role": "advisor", "operation_id": "action-key"})
        self.assertEqual(status, 409)
        self.assertIn("already bound", body["error"])

    def test_after_commit_termination_recovers_without_duplicate_booking(self):
        rid, _ = self.workflow(operation="crash-key")
        def terminate(phase):
            if phase == "after_reservation_commit":
                raise ProcessTerminated()
        self.state.fault_hook = terminate
        with self.assertRaises(ProcessTerminated):
            self.call("POST", f"/api/requests/{rid}/execute", {"operation_id": "crash-key"})
        self.state.fault_hook = None
        current = self.call("GET", f"/api/requests/{rid}")[1]
        self.assertEqual(current["status"], "verifying")
        status, recovered = self.call("POST", f"/api/requests/{rid}/execute", {"operation_id": "crash-key"})
        self.assertEqual(status, 200)
        self.assertEqual(recovered["status"], "completed")
        count = self.state.conn.execute("SELECT COUNT(*) FROM reservations WHERE request_id=?", (rid,)).fetchone()[0]
        self.assertEqual(count, 1)

    def test_precommit_exception_rolls_back_and_returns_controlled_error(self):
        rid, _ = self.workflow(operation="rollback-key")
        def fail(phase):
            if phase == "before_reservation_commit":
                raise RuntimeError("injected")
        self.state.fault_hook = fail
        status, body = self.call("POST", f"/api/requests/{rid}/execute", {"operation_id": "rollback-key"})
        self.assertEqual(status, 502)
        self.assertFalse(self.state.conn.in_transaction)
        self.assertEqual(self.state.conn.execute("SELECT COUNT(*) FROM reservations WHERE request_id=?", (rid,)).fetchone()[0], 0)
        self.assertEqual(self.state.conn.execute("SELECT COUNT(*) FROM operations WHERE operation_id=?", ("rollback-key",)).fetchone()[0], 0)

    def test_ledger_verification_mismatch_does_not_complete(self):
        rid, _ = self.workflow(operation="mismatch-key")
        def tamper(phase):
            if phase == "before_verification":
                self.state.conn.execute("UPDATE reservations SET attendees=attendees+1 WHERE request_id=?", (rid,))
        self.state.fault_hook = tamper
        status, body = self.call("POST", f"/api/requests/{rid}/execute", {"operation_id": "mismatch-key"})
        self.assertEqual(status, 502)
        self.assertEqual(body["status"], "verifying")
        self.assertNotEqual(body["status"], "completed")

    def test_restart_preserves_verifying_workflow_and_reconciles(self):
        rid, _ = self.workflow(operation="restart-key")
        self.state.fault_hook = lambda phase: (_ for _ in ()).throw(ProcessTerminated()) if phase == "after_reservation_commit" else None
        with self.assertRaises(ProcessTerminated):
            self.call("POST", f"/api/requests/{rid}/execute", {"operation_id": "restart-key"})
        db = self.state.db_path
        self.state.close()
        self.state = create_app_state(db)
        status, body = self.call("POST", f"/api/requests/{rid}/execute", {"operation_id": "restart-key"})
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "completed")


if __name__ == "__main__":
    unittest.main()

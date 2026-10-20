import tempfile
import unittest
from pathlib import Path

from app import create_app_state, handle_request
from planner import plan_request


BASE = {
    "event_name": "Robotics workshop",
    "date": "2026-11-05",
    "start_time": "15:00",
    "end_time": "17:00",
    "attendees": 20,
}
EVENT = "Book Student Union room 101 for Robotics workshop on 2026-11-05 from 15:00 to 17:00 for 20 attendees; advisor Dr. Lee."


class Milestone2PlannerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = create_app_state(str(Path(self.tmp.name) / "planner.sqlite3"))

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def plan(self, **overrides):
        requirements = dict(BASE)
        requirements.update(overrides)
        return plan_request(self.state, requirements)

    def call(self, method, path, payload=None):
        return handle_request(self.state, method, path, {} if payload is None else payload)

    def test_capacity_filtering_uses_inventory(self):
        result = self.plan(attendees=80)
        self.assertEqual(result["recommended_plan"]["room_id"], "room-201")
        room101 = next(item for item in result["candidates"] if item["room_id"] == "room-101")
        self.assertIn("insufficient_capacity", room101["hard_constraints_failed"])
        self.assertNotIn(room101, result["feasible_plans"])

    def test_equipment_and_multiple_equipment_filtering(self):
        result = self.plan(required_equipment=["projector", "hybrid camera"])
        self.assertEqual([item["room_id"] for item in result["feasible_plans"]], ["room-201"])
        room101 = next(item for item in result["candidates"] if item["room_id"] == "room-101")
        self.assertEqual(room101["equipment_match"]["missing"], ["hybrid_camera"])
        self.assertIn("missing_equipment", room101["hard_constraints_failed"])

    def test_unknown_equipment_is_explicit_validation_error(self):
        result = self.plan(required_equipment=["teleporter"])
        self.assertTrue(any(error["code"] == "unknown_equipment" for error in result["validation_errors"]))
        self.assertIsNone(result["recommended_plan"])

    def test_unknown_room_is_not_fabricated(self):
        result = self.plan(preferred_room="Student Union room 999")
        self.assertTrue(any(error["code"] == "unknown_room" for error in result["validation_errors"]))
        self.assertIsNone(result["recommended_plan"])

    def test_preferred_room_and_building_ranking(self):
        preferred = self.plan(preferred_room="Library room 201")
        self.assertEqual(preferred["recommended_plan"]["room_id"], "room-201")
        building = self.plan(preferred_building="Engineering")
        self.assertEqual(building["recommended_plan"]["room_id"], "room-301")

    def test_deterministic_tie_breaking_and_hard_vs_soft_preference(self):
        first = self.plan()
        second = self.plan()
        self.assertEqual([item["room_id"] for item in first["feasible_plans"]], [item["room_id"] for item in second["feasible_plans"]])
        self.assertEqual(first["recommended_plan"]["room_id"], "room-101")
        constrained = self.plan(attendees=80, preferred_building="Student Union")
        self.assertEqual(constrained["recommended_plan"]["room_id"], "room-201")
        self.assertTrue(constrained["no_feasible_plan"] is False)

    def test_adjacent_interval_is_available(self):
        result = self.plan(start_time="17:00", end_time="18:00", preferred_room="Student Union room 101")
        candidate = next(item for item in result["candidates"] if item["room_id"] == "room-101")
        self.assertTrue(candidate["feasibility"])

    def test_explicit_flexible_window_is_evaluated_as_a_fallback(self):
        _, created = self.call("POST", "/api/requests", {"text": EVENT})
        rid = created["id"]
        self.call("POST", f"/api/requests/{rid}/plan", {})
        self.call("POST", f"/api/requests/{rid}/submit", {})
        self.call("POST", f"/api/requests/{rid}/approve", {"actor": "advisor-lee", "role": "advisor"})
        self.assertEqual(self.call("POST", f"/api/requests/{rid}/execute", {"operation_id": "flexibility-blocker"})[0], 200)
        result = self.plan(preferred_room="Student Union room 101", flexible_time_window={"start_time": "17:00", "end_time": "18:00"})
        candidate = next(item for item in result["candidates"] if item["room_id"] == "room-101")
        self.assertTrue(candidate["feasibility"])
        self.assertEqual(candidate["selected_interval"]["source"], "flexible")

    def test_legacy_room_shorthand_remains_plannable(self):
        result = self.plan(preferred_room="room 101")
        self.assertEqual(result["recommended_plan"]["room_id"], "room-101")

    def test_flexible_fallback_cannot_execute_without_revised_plan(self):
        _, created = self.call("POST", "/api/requests", {"text": EVENT})
        rid = created["id"]
        self.call("POST", f"/api/requests/{rid}/plan", {})
        self.call("POST", f"/api/requests/{rid}/submit", {})
        self.call("POST", f"/api/requests/{rid}/approve", {"actor": "advisor-lee", "role": "advisor"})
        self.assertEqual(self.call("POST", f"/api/requests/{rid}/execute", {"operation_id": "flexible-blocker"})[0], 200)
        _, created = self.call("POST", "/api/requests", {"text": EVENT})
        other = created["id"]
        self.call("POST", f"/api/requests/{other}/plan", {"flexible_time_window": {"start_time": "17:00", "end_time": "18:00"}})
        self.call("POST", f"/api/requests/{other}/submit", {})
        self.call("POST", f"/api/requests/{other}/approve", {"actor": "advisor-lee", "role": "advisor"})
        status, body = self.call("POST", f"/api/requests/{other}/execute", {"operation_id": "flexible-no-revision"})
        self.assertEqual(status, 409)
        self.assertIn("revised plan", body["error"])

    def test_conflict_fact_changes_plan_after_ledger_write(self):
        _, created = self.call("POST", "/api/requests", {"text": EVENT})
        rid = created["id"]
        self.call("POST", f"/api/requests/{rid}/plan", {})
        self.call("POST", f"/api/requests/{rid}/submit", {})
        self.call("POST", f"/api/requests/{rid}/approve", {"actor": "advisor-lee", "role": "advisor"})
        self.assertEqual(self.call("POST", f"/api/requests/{rid}/execute", {"operation_id": "planner-conflict"})[0], 200)
        result = self.plan(preferred_room="Student Union room 101")
        room101 = next(item for item in result["candidates"] if item["room_id"] == "room-101")
        self.assertFalse(room101["feasibility"])
        self.assertEqual(room101["availability"]["conflicts"][0]["reservation_id"], self.state.conn.execute("SELECT id FROM reservations WHERE request_id=?", (rid,)).fetchone()[0])
        self.assertEqual(result["recommended_plan"]["room_id"], "room-201")

    def test_no_feasible_plan_is_explicit(self):
        result = self.plan(attendees=121)
        self.assertTrue(result["no_feasible_plan"])
        self.assertIsNone(result["recommended_plan"])
        self.assertEqual(len(result["candidates"]), 3)

    def test_ambiguous_or_incomplete_requirements_return_clarification(self):
        result = plan_request(self.state, {"event_name": "Workshop", "attendees": 20})
        self.assertTrue(result["clarification_required"])
        self.assertTrue(result["no_feasible_plan"])
        self.assertTrue(any(error["code"] == "missing_field" for error in result["validation_errors"]))

    def test_invalid_planner_input_is_controlled(self):
        result = plan_request(self.state, {"event_name": "Workshop", "date": "2026-11-05", "start_time": "15:00", "end_time": "17:00", "attendees": True})
        self.assertTrue(any(error["code"] == "invalid_attendees" for error in result["validation_errors"]))
        self.assertTrue(result["no_feasible_plan"])

    def test_planning_is_read_only(self):
        before = {table: self.state.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("requests", "reservations", "operations")}
        self.plan(required_equipment=["projector"])
        after = {table: self.state.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in before}
        self.assertEqual(before, after)

    def test_api_exposes_structured_planning_without_authorizing_execution(self):
        _, created = self.call("POST", "/api/requests", {"text": "Book a room for Robotics workshop on 2026-11-05 from 15:00 to 17:00 for 20 attendees; advisor Dr. Lee."})
        rid = created["id"]
        status, planned = self.call("POST", f"/api/requests/{rid}/plan", {"required_equipment": ["projector"], "preferred_building": "Library"})
        self.assertEqual(status, 200)
        self.assertIn("planning", planned)
        self.assertIn("candidates", planned["planning"])
        self.assertFalse(planned["planning"]["validation_errors"])
        self.assertEqual(planned["status"], "needs_information")
        self.assertEqual(self.call("POST", f"/api/requests/{rid}/execute", {"operation_id": "read-only-plan"})[0], 409)

    def test_execution_revalidates_equipment_after_planning(self):
        _, created = self.call("POST", "/api/requests", {"text": EVENT})
        rid = created["id"]
        self.call("POST", f"/api/requests/{rid}/plan", {"required_equipment": ["hybrid_camera"]})
        self.call("POST", f"/api/requests/{rid}/submit", {})
        self.call("POST", f"/api/requests/{rid}/approve", {"actor": "advisor-lee", "role": "advisor"})
        status, result = self.call("POST", f"/api/requests/{rid}/execute", {"operation_id": "equipment-recheck"})
        self.assertEqual(status, 409)
        self.assertIn("constraints", result["error"])
        self.assertEqual(self.state.conn.execute("SELECT COUNT(*) FROM reservations WHERE request_id=?", (rid,)).fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()

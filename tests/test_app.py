"""Contract tests for the planned CampusRelay application."""

import unittest

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

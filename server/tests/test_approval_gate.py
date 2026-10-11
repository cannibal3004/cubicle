import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, Optional

from app.schemas.contracts import ActionRequest
from app.services.approval_gate import QUESTIONS, ApprovalGate, DeciderError
from app.services.storage_service import StorageService
from app.services.workspace_service import WorkspaceService, WorkspaceToolCall


class ScriptedDecider:
    """A decision server stand-in: answers from a fixed score table, or fails."""

    calls = []

    def __init__(self, base_url: str, timeout: float):
        self.base_url = base_url
        self.timeout = timeout

    scores: Optional[Dict[str, float]] = None
    error: Optional[str] = None

    async def score(self, state: Dict[str, Any], questions: Dict[str, str]) -> Dict[str, float]:
        ScriptedDecider.calls.append({"url": self.base_url, "state": state, "questions": dict(questions)})
        if ScriptedDecider.error:
            raise DeciderError(ScriptedDecider.error)
        return dict(ScriptedDecider.scores or {key: 0.01 for key in questions})


def _request(tool="workspace", action="write", requires_approval=True, arguments=None, target=None, risk="write") -> ActionRequest:
    return ActionRequest(
        request_id="req-gate-test",
        thread_id="thread-test",
        bot_id="bot-test",
        tool=tool,
        action=action,
        intent="test",
        target=target or {},
        arguments=arguments or {},
        preview=f"{tool}.{action}",
        risk=risk,
        requires_approval=requires_approval,
        state="pending_approval" if requires_approval else "approved",
        created_at="2026-10-09T00:00:00+00:00",
    )


class ApprovalGateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.storage = StorageService(self.root / "data")
        self.workspace = WorkspaceService(self.root / "workspace")
        (self.root / "workspace").mkdir()
        self.storage.save_settings({"auto_approval": "on", "decider_url": "http://decider.test:8731", "decider_threshold": 0.2})
        ScriptedDecider.calls = []
        ScriptedDecider.scores = None
        ScriptedDecider.error = None
        self.gate = ApprovalGate(storage=self.storage, workspace=self.workspace, client_factory=ScriptedDecider)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except PermissionError:
            pass  # Windows keeps the SQLite file open until the process exits.

    # ---- mode resolution ------------------------------------------------------

    def test_bot_override_wins_over_app_setting(self):
        self.assertEqual(self.gate.mode_for({"auto_approval": "inherit"}), "on")
        self.assertEqual(self.gate.mode_for({}), "on")
        self.assertEqual(self.gate.mode_for({"auto_approval": "off"}), "off")
        self.assertEqual(self.gate.mode_for({"auto_approval": "shadow"}), "shadow")
        self.storage.save_settings({"auto_approval": "shadow"})
        self.assertEqual(self.gate.mode_for(None), "shadow")
        self.storage.save_settings({"auto_approval": "nonsense"})
        self.assertEqual(self.gate.mode_for(None), "off")

    # ---- what the model is shown ---------------------------------------------

    def test_state_carries_the_action_and_request_but_never_file_content(self):
        (self.root / "workspace" / "notes.txt").write_text("existing private text", encoding="utf-8")
        call = WorkspaceToolCall("workspace.write", "notes.txt", "new private content")
        request = _request(arguments=call.arguments_for_display, target={"path": "notes.txt"})

        state = self.gate.build_state(request, call, "update my notes")

        self.assertEqual(state["user_request"], "update my notes")
        self.assertEqual(state["tool"], "workspace.write")
        self.assertEqual(state["arguments"], {"path": "notes.txt", "bytes": 19})
        self.assertTrue(state["file_exists"])
        self.assertEqual(state["existing_bytes"], 21)
        self.assertNotIn("private", str(state))

    def test_state_redacts_a_pasted_token_in_the_users_message(self):
        token = "github_pat_11AAAAAAA0" + "x" * 60
        request = _request(tool="computer", action="terminal_execute", arguments={"command": f"printf '%s' {token} > /tmp/.gh_token"})
        state = self.gate.build_state(request, None, f"here is my token {token}, push the branch")
        self.assertNotIn(token, state["user_request"])
        self.assertIn("github_pat_[REDACTED]", state["user_request"])
        self.assertIn("push the branch", state["user_request"])

    def test_state_marks_a_new_file_and_clips_long_fields(self):
        call = WorkspaceToolCall("workspace.write", "fresh.txt", "x")
        request = _request(arguments=call.arguments_for_display)
        state = self.gate.build_state(request, call, "y" * 5000)
        self.assertFalse(state["file_exists"])
        self.assertNotIn("existing_bytes", state)
        self.assertLess(len(state["user_request"]), 2100)
        self.assertIn("more characters", state["user_request"])

    # ---- verdicts ------------------------------------------------------------------

    async def test_low_scores_auto_approve_in_on_mode(self):
        ScriptedDecider.scores = {key: 0.05 for key in QUESTIONS}
        verdict = await self.gate.evaluate(_request(), None, mode="on", user_request="run the tests")
        self.assertEqual(verdict.outcome, "auto_approved")
        self.assertTrue(verdict.auto_approved)
        self.assertEqual(verdict.threshold, 0.2)
        self.assertAlmostEqual(verdict.max_score, 0.05)
        self.assertEqual(ScriptedDecider.calls[0]["url"], "http://decider.test:8731")
        self.assertEqual(set(ScriptedDecider.calls[0]["questions"]), set(QUESTIONS))

    async def test_low_scores_only_log_in_shadow_mode(self):
        ScriptedDecider.scores = {key: 0.05 for key in QUESTIONS}
        verdict = await self.gate.evaluate(_request(), None, mode="shadow", user_request="run the tests")
        self.assertEqual(verdict.outcome, "ask")
        self.assertFalse(verdict.auto_approved)
        self.assertIn("shadow", verdict.reason)

    async def test_one_risky_score_means_ask(self):
        ScriptedDecider.scores = {**{key: 0.02 for key in QUESTIONS}, "exfiltrate": 0.91}
        verdict = await self.gate.evaluate(_request(), None, mode="on", user_request="fix the test")
        self.assertEqual(verdict.outcome, "ask")
        self.assertIn("exfiltrate", verdict.reason)
        self.assertAlmostEqual(verdict.max_score, 0.91)

    async def test_score_at_the_threshold_is_not_approved(self):
        ScriptedDecider.scores = {**{key: 0.0 for key in QUESTIONS}, "destroy": 0.2}
        verdict = await self.gate.evaluate(_request(), None, mode="on", user_request="tidy up")
        self.assertEqual(verdict.outcome, "ask")

    async def test_a_shrinking_write_is_held_by_rule_without_the_model(self):
        (self.root / "workspace" / "notes.md").write_text("x" * 400, encoding="utf-8")
        call = WorkspaceToolCall("workspace.write", "notes.md", "y" * 50)
        request = _request(arguments=call.arguments_for_display)
        verdict = await self.gate.evaluate(request, call, mode="on", user_request="tidy the notes")
        self.assertEqual(verdict.outcome, "ask")
        self.assertEqual(verdict.rule, "shrinking_write")
        self.assertEqual(verdict.decided_by, "rule")
        self.assertIn("400 to 50", verdict.reason)
        self.assertEqual(ScriptedDecider.calls, [])
        self.assertEqual(verdict.to_event()["rule"], "shrinking_write")

    async def test_a_write_that_keeps_most_of_the_file_goes_to_the_model(self):
        (self.root / "workspace" / "notes.md").write_text("x" * 400, encoding="utf-8")
        call = WorkspaceToolCall("workspace.write", "notes.md", "y" * 300)
        request = _request(arguments=call.arguments_for_display)
        verdict = await self.gate.evaluate(request, call, mode="on", user_request="tidy the notes")
        self.assertIsNone(verdict.rule)
        self.assertEqual(verdict.outcome, "auto_approved")
        self.assertEqual(len(ScriptedDecider.calls), 1)

    async def test_input_events_are_not_scored(self):
        request = _request(tool="computer", action="send_input", arguments={"event": {"type": "click", "x": 1, "y": 2}})
        on = await self.gate.evaluate(request, None, mode="on", user_request="click the button")
        self.assertEqual((on.outcome, on.rule, on.decided_by), ("auto_approved", "input_event", "rule"))
        shadow = await self.gate.evaluate(request, None, mode="shadow", user_request="click the button")
        self.assertEqual((shadow.outcome, shadow.rule), ("ask", "input_event"))
        self.assertEqual(ScriptedDecider.calls, [])

    async def test_unreachable_server_fails_closed(self):
        ScriptedDecider.error = "decision server unreachable: ConnectError"
        verdict = await self.gate.evaluate(_request(), None, mode="on", user_request="run the tests")
        self.assertEqual(verdict.outcome, "error")
        self.assertFalse(verdict.auto_approved)
        self.assertIn("unreachable", verdict.reason)

    async def test_missing_url_fails_closed_without_calling_anything(self):
        self.storage.save_settings({"decider_url": ""})
        verdict = await self.gate.evaluate(_request(), None, mode="on", user_request="run the tests")
        self.assertEqual(verdict.outcome, "error")
        self.assertEqual(ScriptedDecider.calls, [])

    async def test_off_mode_and_no_approval_needed_are_skipped(self):
        self.assertEqual((await self.gate.evaluate(_request(), None, mode="off", user_request="")).outcome, "skipped")
        self.assertEqual((await self.gate.evaluate(_request(requires_approval=False), None, mode="on", user_request="")).outcome, "skipped")
        self.assertEqual(ScriptedDecider.calls, [])

    # ---- the decision log --------------------------------------------------------

    async def test_decisions_are_logged_with_the_final_answer(self):
        ScriptedDecider.scores = {key: 0.05 for key in QUESTIONS}
        request = _request()
        verdict = await self.gate.evaluate(request, None, mode="on", user_request="run the tests")
        self.gate.record(request, verdict)
        self.gate.record_final(request.request_id, "allow", "decider")

        decisions = self.storage.get_gate_decisions()
        self.assertEqual(len(decisions), 1)
        row = decisions[0]
        self.assertEqual(row["request_id"], "req-gate-test")
        self.assertEqual(row["outcome"], "auto_approved")
        self.assertEqual(row["final_decision"], "allow")
        self.assertEqual(row["decided_by"], "decider")
        self.assertEqual(row["state"]["user_request"], "run the tests")
        self.assertEqual(row["questions"], QUESTIONS)
        self.assertIn("resolved_at", row)

    async def test_check_reports_a_probe_result(self):
        ScriptedDecider.scores = {key: 0.01 for key in QUESTIONS}
        result = await self.gate.check("http://other.test:8731")
        self.assertTrue(result["ok"])
        self.assertEqual(ScriptedDecider.calls[0]["url"], "http://other.test:8731")
        self.assertEqual(ScriptedDecider.calls[0]["state"]["tool"], "workspace.list")
        ScriptedDecider.error = "decision server returned 503"
        self.assertFalse((await self.gate.check("http://other.test:8731"))["ok"])


class GateSettingsSchemaTests(unittest.TestCase):
    def test_settings_schema_validates_the_gate_fields(self):
        from pydantic import ValidationError
        from app.schemas.contracts import AppSettingsSchema

        ok = AppSettingsSchema(auto_approval="shadow", decider_url="http://halogen:8731", decider_threshold=0.35)
        self.assertEqual((ok.auto_approval, ok.decider_url, ok.decider_threshold), ("shadow", "http://halogen:8731", 0.35))
        self.assertEqual(AppSettingsSchema(decider_url="").decider_url, "")
        for bad in ({"auto_approval": "always"}, {"decider_url": "ftp://x"}, {"decider_url": "halogen:8731"},
                    {"decider_threshold": 0}, {"decider_threshold": 1.0}):
            with self.assertRaises(ValidationError, msg=bad):
                AppSettingsSchema(**bad)

    def test_settings_round_trip_through_storage(self):
        temp = tempfile.TemporaryDirectory()
        storage = StorageService(Path(temp.name))
        self.assertEqual(storage.get_settings()["auto_approval"], "off")
        storage.save_settings({"auto_approval": "on", "decider_url": "http://h:8731", "decider_threshold": 0.15})
        public = storage.get_public_settings()
        self.assertEqual((public["auto_approval"], public["decider_url"], public["decider_threshold"]), ("on", "http://h:8731", 0.15))
        try:
            temp.cleanup()
        except PermissionError:
            pass


if __name__ == "__main__":
    unittest.main()

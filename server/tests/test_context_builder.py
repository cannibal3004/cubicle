import json
import unittest
from unittest import mock

from app.services import context_builder
from app.services.context_builder import (
    SUMMARY_LEAD,
    build_history_messages,
    cap_message_results,
    trim_result_text,
)


def _bot(text, calls=None):
    return {"sender": "bot", "text": text, "raw_payload": {"tool_calls": calls} if calls else None}


def _user(text):
    return {"sender": "user", "text": text}


def _compaction(text):
    return {"sender": "system", "item_type": "compaction", "text": text}


class ContextBuilderTests(unittest.TestCase):
    def test_results_are_trimmed_and_capped_per_message(self):
        with mock.patch.object(context_builder.settings, "CONTEXT_TOOL_RESULT_CHARS", 10):
            self.assertEqual(trim_result_text("short"), "short")
            self.assertTrue(trim_result_text("x" * 50).startswith("x" * 10 + "\n...[trimmed 40 characters]"))
        records = [{"id": "a", "result": "1" * 30}, {"id": "b", "result": "2" * 30}, {"id": "c", "result": "3" * 30}]
        with mock.patch.object(context_builder.settings, "CONTEXT_TOOL_RESULTS_MESSAGE_CHARS", 65):
            cap_message_results(records)
        # The oldest result goes first; the newer two still fit.
        self.assertIsNone(records[0]["result"])
        self.assertTrue(records[0]["result_dropped"])
        self.assertEqual(records[1]["result"], "2" * 30)
        self.assertEqual(records[2]["result"], "3" * 30)

    def test_every_tool_turn_replays_its_full_results(self):
        history = [
            _user("find it"),
            _bot("Found A.", [{"id": "c1", "name": "exa_search", "raw_arguments": '{"query": "A"}', "status": "completed", "summary": "Search A", "result": '{"hits": ["A"]}'}]),
            _user("and B?"),
            _bot("Found B.", [{"id": "c2", "name": "exa_search", "arguments": {"query": "B"}, "status": "completed", "summary": "Search B", "result": '{"hits": ["B"]}'},
                             {"id": "c3", "name": "gmail_send_email", "status": "denied", "summary": "Send mail", "error": "The user denied this action"}]),
            _user("thanks"),
            _bot("You're welcome."),
        ]
        messages = build_history_messages(history, replay_tools=True)

        roles = [(m["role"], m.get("content")[:12] if isinstance(m.get("content"), str) else "") for m in messages]
        self.assertEqual(
            roles,
            [("user", "find it"), ("assistant", ""), ("tool", '{"hits": ["A'), ("assistant", "Found A."),
             ("user", "and B?"), ("assistant", ""), ("tool", '{"hits": ["B'), ("tool", '{"status": "'), ("assistant", "Found B."),
             ("user", "thanks"), ("assistant", "You're welco")],
        )
        # The older turn keeps its real result: the context is never rewritten after it was sent.
        self.assertEqual(messages[2]["content"], '{"hits": ["A"]}')
        self.assertEqual(messages[1]["tool_calls"], [{"id": "c1", "name": "exa_search", "arguments": '{"query": "A"}'}])
        # Arguments stored as a dict are serialised; a denied call explains itself.
        self.assertEqual(messages[5]["tool_calls"][0]["arguments"], '{"query": "B"}')
        denied = json.loads(messages[7]["content"])
        self.assertEqual(denied["status"], "denied")
        self.assertIn("denied", denied["error"])

    def test_the_same_history_builds_the_same_messages_as_it_grows(self):
        calls = [{"id": "c1", "name": "t", "status": "completed", "result": "R"}]
        history = [_user("a"), _bot("A", calls), _user("b"), _bot("B", calls), _user("c"), _bot("C", calls), _user("d"), _bot("D", calls)]
        before = build_history_messages(history[:6], replay_tools=True)
        after = build_history_messages(history, replay_tools=True)
        self.assertEqual(after[: len(before)], before)

    def test_without_tool_replay_only_texts_are_used(self):
        history = [_user("hi"), _bot("Found A.", [{"id": "c1", "name": "t", "status": "completed", "result": "big"}])]
        messages = build_history_messages(history, replay_tools=False)
        self.assertEqual([(m["role"], m["content"]) for m in messages], [("user", "hi"), ("assistant", "Found A.")])

    def test_records_without_ids_or_dropped_results_degrade_gracefully(self):
        history = [
            _user("go"),
            _bot("Done.", [{"name": "no-id", "status": "completed", "result": "x"},
                           {"id": "c9", "name": "t", "status": "completed", "result": None, "result_dropped": True}]),
        ]
        messages = build_history_messages(history, replay_tools=True)
        self.assertEqual([m["role"] for m in messages], ["user", "assistant", "tool", "assistant"])
        self.assertEqual(json.loads(messages[2]["content"])["note"], "result not retained")
        self.assertEqual(len(messages[1]["tool_calls"]), 1)

    def test_user_edits_of_the_notes_are_shown_where_they_happened(self):
        from app.services.context_builder import MEMORY_EDIT_LEAD

        history = [
            _user("start"), _bot("Started."),
            {"sender": "system", "item_type": "memory", "text": "Goal: ship v2."},
            _user("go on"), _bot("Going."),
            {"sender": "system", "item_type": "memory", "text": ""},
            _user("and?"),
        ]
        messages = build_history_messages(history, replay_tools=True)
        self.assertEqual([m["role"] for m in messages], ["user", "assistant", "user", "assistant", "user", "assistant", "user", "assistant", "user"])
        self.assertEqual(messages[2]["content"], f"{MEMORY_EDIT_LEAD}\n\nGoal: ship v2.")
        self.assertEqual(messages[3]["content"], "Noted.")
        self.assertIn("cleared your notes", messages[6]["content"])

    def test_a_compaction_carries_the_notes_snapshot(self):
        from app.services.context_builder import MEMORY_LEAD

        history = [
            _user("old"), _bot("Old."),
            {"sender": "system", "item_type": "compaction", "text": "summary", "raw_payload": {"memory": "Repo: ~/proj"}},
            _user("recent"),
        ]
        messages = build_history_messages(history, replay_tools=True)
        self.assertEqual(messages[0]["content"], f"{SUMMARY_LEAD}\n\nsummary\n\n{MEMORY_LEAD}\n\nRepo: ~/proj")
        # Without notes the summary message is unchanged.
        history[2]["raw_payload"] = {"memory": ""}
        self.assertEqual(build_history_messages(history, replay_tools=True)[0]["content"], f"{SUMMARY_LEAD}\n\nsummary")

    def test_context_starts_at_the_latest_compaction(self):
        history = [
            _user("old 1"), _bot("Old 1."), _compaction("first summary"),
            _user("old 2"), _bot("Old 2."), _compaction("second summary"),
            _user("recent"), _bot("Recent."), {"sender": "system", "text": "a local note that is not a compaction"},
        ]
        messages = build_history_messages(history, replay_tools=True)
        self.assertEqual([m["role"] for m in messages], ["user", "assistant", "user", "assistant"])
        self.assertEqual(messages[0]["content"], f"{SUMMARY_LEAD}\n\nsecond summary")
        self.assertNotIn("first summary", json.dumps(messages))
        self.assertNotIn("old", json.dumps(messages))
        self.assertEqual(messages[2]["content"], "recent")


if __name__ == "__main__":
    unittest.main()

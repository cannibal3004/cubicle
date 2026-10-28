"""Thinking control: where a turn's reasoning effort comes from, Auto's choice, and its log."""

import asyncio
import unittest
from unittest import mock

from app.routers import bots as bots_router
from app.routers import conversations as conversations_router
from app.services import conversations, thinking
from app.services.approval_gate import DeciderError, approval_gate

import test_chat_router as chat_harness


def _decider(scores=None, error=None, seen=None):
    """A decision model that answers every question with `scores`, or fails."""

    class Client:
        def __init__(self, url, timeout):
            pass

        async def score(self, state, questions):
            if seen is not None:
                seen.append((state, sorted(questions)))
            if error:
                raise DeciderError(error)
            return dict(scores)

    return Client


class SettingTests(unittest.TestCase):
    def test_settings_are_cleaned_and_the_conversation_wins_over_the_bot_over_the_app(self):
        self.assertEqual([thinking.clean_setting(v) for v in ("", None, "default", "off", "AUTO", "high")], ["", "", "", "none", "auto", "high"])
        with self.assertRaises(thinking.ThinkingError):
            thinking.clean_setting("extreme")
        self.assertEqual(thinking.configured({}, {}, "xhigh"), ("xhigh", "app"))
        self.assertEqual(thinking.configured({"thinking": "low"}, {}, "xhigh"), ("low", "bot"))
        self.assertEqual(thinking.configured({"thinking": "low"}, {"thinking": "auto"}, "xhigh"), ("auto", "conversation"))
        # Auto's standing level (compactions, before the pick) is where the conversation last was.
        self.assertEqual(thinking.standing_level({"thinking": "auto"}, {"thinking_last": "low"}, ""), "low")
        self.assertEqual(thinking.standing_level({"thinking": "auto"}, {}, ""), "xhigh")
        # The menus offer what THINKING_LEVELS lists, in order.
        with mock.patch.object(thinking.settings, "THINKING_LEVELS", "xhigh, low,medium,none"):
            self.assertEqual(thinking.offered_levels(), ["none", "low", "medium", "xhigh"])

    def test_auto_leans_deep_and_is_sticky(self):
        # First pick: light only below 0.3.
        self.assertEqual(thinking.decide({"complex": 0.05, "stakes": 0.22}, "")[0], "low")
        self.assertEqual(thinking.decide({"complex": 0.32, "stakes": 0.0}, "")[0], "xhigh")
        # After a deep turn it takes a clear no to go light; after a light turn a clear yes to go deep.
        self.assertEqual(thinking.decide({"complex": 0.12, "stakes": 0.0}, "xhigh")[0], "xhigh")
        self.assertEqual(thinking.decide({"complex": 0.05, "stakes": 0.02}, "xhigh")[0], "low")
        self.assertEqual(thinking.decide({"complex": 0.35, "stakes": 0.0}, "low")[0], "low")
        self.assertEqual(thinking.decide({"complex": 0.6, "stakes": 0.0}, "low")[0], "xhigh")

    def test_the_decider_sees_the_message_and_the_exchange_before_it_never_tool_output(self):
        history = [
            {"sender": "user", "text": "Plan a migration of the NAS to ZFS."},
            {"sender": "bot", "text": "Here is a five-step plan..."},
            {"sender": "system", "text": "tool output that must not be shown"},
            {"sender": "user", "text": "do it"},
        ]
        state = thinking.build_state(history)
        self.assertEqual(state, {"message": "do it", "previous_reply": "Here is a five-step plan...", "previous_message": "Plan a migration of the NAS to ZFS."})

    def test_auto_falls_back_to_where_it_was_or_deep(self):
        history = [{"sender": "user", "text": "thanks!"}]
        bot = {"thinking": "auto"}
        seen = []
        with mock.patch.object(approval_gate, "_config", return_value={"url": "http://decider.test", "threshold": 0.2}), \
             mock.patch.object(approval_gate, "client_factory", _decider({"complex": 0.02, "stakes": 0.01}, seen=seen)):
            pick = asyncio.run(thinking.choose(bot, {}, history, "xhigh"))
        self.assertEqual((pick.level, pick.auto, pick.source), ("low", True, "bot"))
        self.assertEqual(seen[0][1], ["complex", "stakes"])
        with mock.patch.object(approval_gate, "_config", return_value={"url": "http://decider.test", "threshold": 0.2}), \
             mock.patch.object(approval_gate, "client_factory", _decider(error="unreachable")):
            self.assertEqual(asyncio.run(thinking.choose(bot, {"thinking_last": "low"}, history, "")).level, "low")
            self.assertEqual(asyncio.run(thinking.choose(bot, {}, history, "")).level, "xhigh")
        with mock.patch.object(approval_gate, "_config", return_value={"url": "", "threshold": 0.2}):
            pick = asyncio.run(thinking.choose(bot, {}, history, ""))
        self.assertEqual((pick.level, pick.reason), ("xhigh", "no decision server configured"))
        # A fixed setting asks nobody.
        self.assertEqual(asyncio.run(thinking.choose({"thinking": "medium"}, {}, history, "")).level, "medium")


class ThinkingTurnTests(chat_harness.ChatStreamRouteTests):
    def setUp(self):
        super().setUp()
        self.bot = self.storage.get_bots()[0]["id"]
        self.patch = mock.patch.object(chat_harness.storage_module, "storage_service", self.storage)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        super().tearDown()

    def _turn(self, text, thread_id=None, scores=None):
        thread_id = thread_id or self.bot
        self.storage.add_message({"id": f"u-{len(self.storage.get_messages(thread_id))}", "thread_id": thread_id, "bot_id": self.bot, "sender": "user", "text": text})
        sent = {}

        async def stream(model, messages, system_prompt="", config=None, tools=None, one_shot=False):
            sent["effort"] = config.reasoning_effort
            yield {"type": "content.delta", "delta": "Done."}
            yield {"type": "turn.completed", "ok": True}

        with mock.patch.object(approval_gate, "_config", return_value={"url": "http://decider.test", "threshold": 0.2}), \
             mock.patch.object(approval_gate, "client_factory", _decider(scores or {"complex": 0.9, "stakes": 0.0})):
            _, events = self._run_turn(thread_id, stream=stream)
        return sent["effort"], events

    def test_the_turn_sends_the_chosen_effort_shows_it_and_logs_how_it_went(self):
        # Nothing configured: the app's level (here none) is sent, and the reply shows nothing.
        effort, _ = self._turn("hi")
        self.assertEqual(effort, "")
        self.assertNotIn("thinking", self.storage.get_messages(self.bot)[-1].get("raw_payload") or {})

        asyncio.run(bots_router.update_bot(self.bot, bots_router.BotUpdate(thinking="medium")))
        effort, events = self._turn("hello again")
        self.assertEqual(effort, "medium")
        picked = next(e for e in events if e["type"] == "thinking.picked")
        self.assertEqual((picked["level"], picked["auto"], picked["source"]), ("medium", False, "bot"))

        # /think auto for this conversation: the decider says deep, then (sticky) a clear no goes light.
        asyncio.run(conversations_router.update_conversation(self.bot, conversations_router.ConversationUpdate(thinking="auto")))
        effort, _ = self._turn("debug this failing build", scores={"complex": 0.9, "stakes": 0.2})
        self.assertEqual(effort, "xhigh")
        effort, _ = self._turn("thanks!", scores={"complex": 0.02, "stakes": 0.01})
        self.assertEqual(effort, "low")
        self.assertEqual(conversations.get(self.bot)["thinking_last"], "low")
        reply = self.storage.get_messages(self.bot)[-1]
        self.assertEqual((reply["raw_payload"]["thinking"]["level"], reply["raw_payload"]["thinking"]["auto"]), ("low", True))

        log = thinking.recent()
        self.assertEqual([p["level"] for p in log[:4]], ["low", "xhigh", "medium", ""])
        self.assertEqual((log[0]["outcome"]["rounds"], log[0]["outcome"]["cancelled"]), (1, False))
        self.assertEqual(log[0]["message_id"], reply["id"])
        # Taking the reply back marks its pick.
        from app.services import rewind

        rewind.rewind(self.bot, self.storage.get_messages(self.bot)[-2]["id"])
        self.assertTrue(thinking.recent()[0].get("undone"))

    def test_the_hint_style_sends_one_level_and_says_the_rest_at_the_end_of_the_message(self):
        with mock.patch.object(thinking.settings, "THINKING_STYLE", "field"):
            self.assertEqual(thinking.wire("low"), ("low", None))
        with mock.patch.object(thinking.settings, "THINKING_STYLE", "hint"):
            self.assertEqual(thinking.wire("low"), ("medium", thinking.HINTS["low"]))
            self.assertEqual(thinking.wire("high"), ("medium", thinking.HINTS["xhigh"]))  # folded as halogen folds it
            self.assertEqual(thinking.wire("medium"), ("medium", None))
            self.assertEqual(thinking.wire("none"), ("none", None))  # off is a different switch
            self.assertEqual(thinking.wire(""), ("", None))
            # Auto follows the decider every message: a change costs nothing.
            self.assertEqual(thinking.decide({"complex": 0.2, "stakes": 0.0}, "xhigh", sticky=False)[0], "low")

            sent = []

            async def stream(model, messages, system_prompt="", config=None, tools=None, one_shot=False):
                sent.append((config.reasoning_effort, [m["content"] for m in messages if m.get("role") == "user"]))
                yield {"type": "content.delta", "delta": "Done."}
                yield {"type": "turn.completed", "ok": True}

            asyncio.run(bots_router.update_bot(self.bot, bots_router.BotUpdate(thinking="low")))
            self.storage.add_message({"id": "h1", "thread_id": self.bot, "bot_id": self.bot, "sender": "user", "text": "quick question"})
            self._run_turn(self.bot, stream=stream)
            asyncio.run(conversations_router.update_conversation(self.bot, conversations_router.ConversationUpdate(thinking="xhigh")))
            self.storage.add_message({"id": "h2", "thread_id": self.bot, "bot_id": self.bot, "sender": "user", "text": "now think hard"})
            self._run_turn(self.bot, stream=stream)
        (first_effort, first), (second_effort, second) = sent[0], sent[-1]
        self.assertEqual((first_effort, second_effort), ("medium", "medium"))  # one cache for the conversation
        self.assertTrue(first[-1].endswith(thinking.HINTS["low"]))
        self.assertTrue(second[-1].endswith(thinking.HINTS["xhigh"]))
        # The earlier message is replayed exactly as it was first sent, its hint included.
        self.assertEqual(second[-2], first[-1])
        stored = {m["id"]: m for m in self.storage.get_messages(self.bot)}
        self.assertEqual(stored["h1"]["text"], "quick question")  # the user's words are untouched
        self.assertEqual(stored["h1"]["raw_payload"]["thinking_hint"], thinking.HINTS["low"])
        reply = self.storage.get_messages(self.bot)[-1]
        self.assertEqual((reply["raw_payload"]["thinking"]["level"], reply["raw_payload"]["thinking"]["sent"]), ("xhigh", "medium"))

    def test_a_bad_setting_is_refused(self):
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(bots_router.update_bot(self.bot, bots_router.BotUpdate(thinking="extreme")))
        self.assertEqual(ctx.exception.status_code, 422)


if __name__ == "__main__":
    unittest.main()

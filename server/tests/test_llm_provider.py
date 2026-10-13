import asyncio
import json
import unittest
from typing import List
from unittest import mock

import httpx

from app.services import llm_config as llm_config_module
from app.services import llm_service
from app.services.llm_config import LLMConfig, resolve_llm_config
from app.services.openai_compatible_service import (
    OpenAICompatibleService,
    build_headers,
    build_messages,
    build_request_body,
)


def _config(**overrides) -> LLMConfig:
    values = {
        "provider": "openai_compatible",
        "api_key": "sk-test",
        "base_url": "https://example.test/v1",
        "reasoning_effort": "",
        "default_model": "test/default-model",
    }
    values.update(overrides)
    return LLMConfig(**values)


def _service_with_handler(handler) -> OpenAICompatibleService:
    def factory(timeout):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=timeout)

    return OpenAICompatibleService(client_factory=factory)


def _sse(*chunks) -> bytes:
    lines = [": OPENROUTER PROCESSING", ""]
    for chunk in chunks:
        lines.append(f"data: {json.dumps(chunk)}")
        lines.append("")
    lines.extend(["data: [DONE]", ""])
    return "\n".join(lines).encode("utf-8")


def _collect(generator) -> List[dict]:
    async def run():
        return [event async for event in generator]

    return asyncio.run(run())


class LLMConfigTests(unittest.TestCase):
    def test_saved_settings_override_environment_and_fill_provider_defaults(self):
        with mock.patch.multiple(
            llm_config_module.settings,
            LLM_PROVIDER="openai_compatible",
            LLM_API_KEY="env-key",
            LLM_BASE_URL="",
            LLM_REASONING_EFFORT="",
            DEFAULT_MODEL="env/model",
        ):
            config = resolve_llm_config({})
            self.assertEqual(config.base_url, "https://openrouter.ai/api/v1")
            self.assertEqual(config.api_key, "env-key")
            self.assertEqual(config.default_model, "env/model")
            self.assertTrue(config.is_openrouter)

            config = resolve_llm_config(
                {
                    "llm_provider": "muapi",
                    "llm_api_key": "saved-key",
                    "llm_base_url": "https://muapi.example/api/v1/",
                    "llm_reasoning_effort": "HIGH",
                    "default_model": "grok-4-5",
                }
            )
            self.assertEqual(config.provider, "muapi")
            self.assertEqual(config.api_key, "saved-key")
            self.assertEqual(config.base_url, "https://muapi.example/api/v1")
            self.assertEqual(config.reasoning_effort, "high")
            self.assertFalse(config.is_openrouter)

    def test_unknown_provider_and_effort_fall_back_safely(self):
        with mock.patch.multiple(
            llm_config_module.settings,
            LLM_PROVIDER="openai_compatible",
            LLM_API_KEY="",
            LLM_BASE_URL="",
            LLM_REASONING_EFFORT="",
            DEFAULT_MODEL="",
        ):
            config = resolve_llm_config(
                {"llm_provider": "something-else", "llm_reasoning_effort": "very high!"}
            )
        self.assertEqual(config.provider, "openai_compatible")
        self.assertEqual(config.reasoning_effort, "")

    def test_unlisted_but_well_formed_effort_values_pass_through(self):
        # halogen-flash-server accepts xhigh and max; other servers may add
        # more. The server, not this app, decides what it understands.
        with mock.patch.multiple(
            llm_config_module.settings,
            LLM_PROVIDER="openai_compatible",
            LLM_API_KEY="",
            LLM_BASE_URL="",
            LLM_REASONING_EFFORT="",
            DEFAULT_MODEL="",
        ):
            for value in ("xhigh", "max", "Turbo-2"):
                config = resolve_llm_config({"llm_reasoning_effort": value})
                self.assertEqual(config.reasoning_effort, value.lower())
        self.assertEqual(config.api_key, "")
        self.assertEqual(config.default_model, "x-ai/grok-4.5")


class RequestBuildingTests(unittest.TestCase):
    def test_messages_become_openai_shape_with_image_parts(self):
        history = [
            {"role": "user", "content": "Describe this", "image_url": "data:image/png;base64,AAAA"},
            {"role": "assistant", "content": "A picture."},
            {"role": "assistant", "content": "   "},
            {"role": "system", "content": "ignored, the router passes the prompt separately"},
            {"role": "user", "content": "Thanks"},
        ]
        messages = build_messages(history, "  Be terse.  ")
        self.assertEqual(messages[0], {"role": "system", "content": "Be terse."})
        self.assertEqual(messages[1]["role"], "user")
        self.assertEqual(messages[1]["content"][0], {"type": "text", "text": "Describe this"})
        self.assertEqual(
            messages[1]["content"][1],
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        )
        self.assertEqual(messages[2], {"role": "assistant", "content": "A picture."})
        self.assertEqual(messages[3], {"role": "user", "content": "Thanks"})
        self.assertEqual(len(messages), 4)

    def test_messages_carry_tool_calls_and_tool_results(self):
        history = [
            {"role": "user", "content": "List the workspace"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call_1", "name": "workspace_list", "arguments": {"path": "."}}],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "{\"entries\": []}"},
            {"role": "user", "content": [{"type": "text", "text": "[Screenshot]"}, {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}}]},
        ]
        messages = build_messages(history, "sys")
        self.assertEqual(messages[1], {"role": "user", "content": "List the workspace"})
        self.assertEqual(messages[2]["role"], "assistant")
        self.assertEqual(messages[2]["content"], "")
        self.assertEqual(
            messages[2]["tool_calls"],
            [{"id": "call_1", "type": "function", "function": {"name": "workspace_list", "arguments": "{\"path\": \".\"}"}}],
        )
        self.assertEqual(messages[3], {"role": "tool", "tool_call_id": "call_1", "content": "{\"entries\": []}"})
        self.assertEqual(messages[4]["content"][1]["type"], "image_url")

    def test_tools_are_only_sent_when_provided(self):
        history = [{"role": "user", "content": "hi"}]
        self.assertNotIn("tools", build_request_body("m", history, "", _config()))
        tools = [{"type": "function", "function": {"name": "x", "parameters": {"type": "object", "properties": {}}}}]
        self.assertEqual(build_request_body("m", history, "", _config(), tools=tools)["tools"], tools)

    def test_reasoning_effort_is_only_sent_when_configured(self):
        history = [{"role": "user", "content": "hi"}]
        body = build_request_body("m", history, "", _config())
        self.assertNotIn("reasoning_effort", body)
        self.assertTrue(body["stream"])

        body = build_request_body("m", history, "", _config(reasoning_effort="low"))
        self.assertEqual(body["reasoning_effort"], "low")

    def test_headers_omit_auth_for_keyless_local_servers(self):
        local = build_headers(_config(api_key="", base_url="http://localhost:11434/v1"))
        self.assertNotIn("Authorization", local)
        self.assertNotIn("X-Title", local)

        remote = build_headers(_config(base_url="https://openrouter.ai/api/v1"))
        self.assertEqual(remote["Authorization"], "Bearer sk-test")
        self.assertEqual(remote["X-Title"], "Cubicle")


class StreamingTests(unittest.TestCase):
    def test_streams_reasoning_and_content_as_separate_events(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["body"] = json.loads(request.content)
            captured["auth"] = request.headers.get("Authorization")
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=_sse(
                    {"choices": [{"delta": {"reasoning": "thinking"}}]},
                    {"choices": [{"delta": {"reasoning_content": "..."}}]},
                    {"choices": [{"delta": {"content": "Hel"}}]},
                    {"choices": [{"delta": {"content": "lo"}, "finish_reason": "stop"}]},
                    {"choices": [], "usage": {"prompt_tokens": 120, "completion_tokens": 7, "total_tokens": 127}},
                ),
            )

        service = _service_with_handler(handler)
        events = _collect(
            service.stream_chat_completion(
                "test/model",
                [{"role": "user", "content": "hi"}],
                "sys",
                _config(reasoning_effort="medium"),
            )
        )

        self.assertEqual(captured["url"], "https://example.test/v1/chat/completions")
        self.assertEqual(captured["auth"], "Bearer sk-test")
        self.assertEqual(captured["body"]["model"], "test/model")
        self.assertEqual(captured["body"]["reasoning_effort"], "medium")
        self.assertEqual(captured["body"]["stream_options"], {"include_usage": True})
        self.assertEqual(
            events,
            [
                {"type": "reasoning.delta", "delta": "thinking"},
                {"type": "reasoning.delta", "delta": "..."},
                {"type": "content.delta", "delta": "Hel"},
                {"type": "content.delta", "delta": "lo"},
                {"type": "usage", "prompt_tokens": 120, "completion_tokens": 7, "total_tokens": 127},
                {"type": "turn.completed", "ok": True},
            ],
        )

    def test_streamed_tool_call_fragments_are_assembled(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=_sse(
                    {"choices": [{"delta": {"content": "Let me check."}}]},
                    {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "type": "function", "function": {"name": "workspace_list", "arguments": ""}}]}}]},
                    {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "{\"pa"}}]}}]},
                    {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "th\": \".\"}"}}]}}]},
                    {"choices": [{"delta": {"tool_calls": [{"index": 1, "id": "call_2", "function": {"name": "computer_screenshot", "arguments": "{}"}}]}}]},
                    {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
                ),
            )

        tools = [{"type": "function", "function": {"name": "workspace_list", "parameters": {"type": "object", "properties": {}}}}]
        events = _collect(
            _service_with_handler(handler).stream_chat_completion(
                "m", [{"role": "user", "content": "hi"}], "", _config(), tools=tools
            )
        )
        self.assertEqual(captured["body"]["tools"], tools)
        self.assertEqual(
            events,
            [
                {"type": "content.delta", "delta": "Let me check."},
                {
                    "type": "tool_calls",
                    "calls": [
                        {"id": "call_1", "name": "workspace_list", "arguments": "{\"path\": \".\"}"},
                        {"id": "call_2", "name": "computer_screenshot", "arguments": "{}"},
                    ],
                },
                {"type": "turn.completed", "ok": True},
            ],
        )

    def test_non_streaming_tool_calls_are_reported(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "workspace_read", "arguments": "{\"path\":\"a\"}"}}],
                            }
                        }
                    ]
                },
            )

        events = _collect(
            _service_with_handler(handler).stream_chat_completion("m", [{"role": "user", "content": "hi"}], "", _config())
        )
        self.assertEqual(
            events,
            [
                {"type": "tool_calls", "calls": [{"id": "c1", "name": "workspace_read", "arguments": "{\"path\":\"a\"}"}]},
                {"type": "turn.completed", "ok": True},
            ],
        )

    def test_reasoning_alone_is_not_a_valid_answer(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=_sse({"choices": [{"delta": {"reasoning_content": "only thinking"}}]}),
            )

        events = _collect(
            _service_with_handler(handler).stream_chat_completion(
                "m", [{"role": "user", "content": "hi"}], "", _config()
            )
        )
        self.assertEqual(events[0], {"type": "reasoning.delta", "delta": "only thinking"})
        self.assertIn("empty response", events[1]["delta"])
        self.assertEqual(events[2], {"type": "turn.completed", "ok": False})

    def test_http_error_body_is_surfaced_to_the_user(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                400,
                json={"error": {"message": "Unrecognized request argument: reasoning_effort"}},
            )

        events = _collect(
            _service_with_handler(handler).stream_chat_completion(
                "m", [{"role": "user", "content": "hi"}], "", _config(reasoning_effort="high")
            )
        )
        self.assertEqual(len(events), 2)
        self.assertIn("HTTP 400", events[0]["delta"])
        self.assertIn("reasoning_effort", events[0]["delta"])
        self.assertEqual(events[1], {"type": "turn.completed", "ok": False})

    def test_mid_stream_error_object_stops_the_turn(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=_sse(
                    {"choices": [{"delta": {"content": "Partial"}}]},
                    {"error": {"message": "Provider returned error", "code": 502}},
                ),
            )

        events = _collect(
            _service_with_handler(handler).stream_chat_completion(
                "m", [{"role": "user", "content": "hi"}], "", _config()
            )
        )
        self.assertEqual(events[0]["delta"], "Partial")
        self.assertEqual(events[1]["delta"], "Error: Provider returned error")
        self.assertEqual(events[2], {"type": "turn.completed", "ok": False})

    def test_non_streaming_json_reply_is_still_accepted(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "reasoning_content": "Let me think.",
                                "content": "Whole answer",
                            }
                        }
                    ]
                },
            )

        events = _collect(
            _service_with_handler(handler).stream_chat_completion(
                "m", [{"role": "user", "content": "hi"}], "", _config()
            )
        )
        self.assertEqual(
            events,
            [
                {"type": "reasoning.delta", "delta": "Let me think."},
                {"type": "content.delta", "delta": "Whole answer"},
                {"type": "turn.completed", "ok": True},
            ],
        )

    def test_connection_failure_is_reported_not_raised(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        events = _collect(
            _service_with_handler(handler).stream_chat_completion(
                "m", [{"role": "user", "content": "hi"}], "", _config(base_url="http://localhost:1/v1")
            )
        )
        self.assertIn("could not reach http://localhost:1/v1/chat/completions", events[0]["delta"])
        self.assertEqual(events[1], {"type": "turn.completed", "ok": False})


class ModelListingTests(unittest.TestCase):
    def test_openrouter_style_listing_is_normalised_and_cached(self):
        calls = {"count": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["count"] += 1
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "x-ai/grok-4.5",
                            "name": "xAI: Grok 4.5",
                            "description": "d" * 300,
                            "context_length": 256000,
                            "supported_parameters": ["reasoning", "temperature"],
                            "architecture": {"input_modalities": ["text", "image"]},
                        },
                        {
                            "id": "anthropic/claude-sonnet-4.5",
                            "name": "Anthropic: Claude Sonnet 4.5",
                            "supported_parameters": ["temperature"],
                            "architecture": {"modality": "text->text"},
                        },
                        {"id": "x-ai/grok-4.5"},
                        {"object": "model"},
                    ]
                },
            )

        service = _service_with_handler(handler)
        config = _config(default_model="x-ai/grok-4.5")
        models = asyncio.run(service.list_models(config))
        asyncio.run(service.list_models(config))
        self.assertEqual(calls["count"], 1)

        self.assertEqual([m.id for m in models], ["anthropic/claude-sonnet-4.5", "x-ai/grok-4.5"])
        grok = models[1]
        self.assertEqual(grok.provider, "x-ai")
        self.assertTrue(grok.recommended)
        self.assertTrue(grok.supports_reasoning)
        self.assertTrue(grok.supports_vision)
        self.assertEqual(grok.context_length, 256000)
        self.assertTrue(grok.description.endswith("..."))
        self.assertLessEqual(len(grok.description), 240)
        claude = models[0]
        self.assertFalse(claude.supports_reasoning)
        self.assertFalse(claude.supports_vision)
        self.assertIsNone(claude.context_length)

        asyncio.run(service.list_models(config, refresh=True))
        self.assertEqual(calls["count"], 2)

    def test_local_server_listing_without_capability_hints(self):
        def handler(request: httpx.Request) -> httpx.Response:
            self_check = request.headers.get("Authorization")
            assert self_check is None
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": "llama3.2:3b", "object": "model", "owned_by": "library"},
                        {
                            "id": "qwen2.5-coder-7b",
                            "object": "model",
                            "owned_by": "llamacpp",
                            "meta": {"n_ctx_train": 32768},
                        },
                    ]
                },
            )

        config = _config(api_key="", base_url="http://localhost:11434/v1", default_model="llama3.2:3b")
        models = asyncio.run(_service_with_handler(handler).list_models(config))
        by_id = {m.id: m for m in models}
        self.assertEqual(by_id["llama3.2:3b"].provider, "Local")
        self.assertIsNone(by_id["llama3.2:3b"].supports_reasoning)
        self.assertIsNone(by_id["llama3.2:3b"].supports_vision)
        self.assertEqual(by_id["qwen2.5-coder-7b"].provider, "llamacpp")
        self.assertEqual(by_id["qwen2.5-coder-7b"].context_length, 32768)

    def test_catalog_falls_back_to_default_model_when_server_is_unreachable(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        service = _service_with_handler(handler)
        config = _config(base_url="http://localhost:1/v1", default_model="my-model")
        with mock.patch.object(llm_service, "openai_compatible_service", service):
            catalog = asyncio.run(llm_service.list_models(config))
        self.assertEqual(catalog.source, "fallback")
        self.assertIn("refused", catalog.error)
        self.assertEqual([m.id for m in catalog.models], ["my-model"])
        self.assertFalse(catalog.models[0].is_available)

    def test_muapi_provider_uses_static_catalog(self):
        catalog = asyncio.run(
            llm_service.list_models(_config(provider="muapi", default_model="claude-opus-5"))
        )
        self.assertEqual(catalog.source, "static")
        recommended = [m.id for m in catalog.models if m.recommended]
        self.assertEqual(recommended, ["claude-opus-5"])


if __name__ == "__main__":
    unittest.main()

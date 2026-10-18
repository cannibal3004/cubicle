"""Legacy MUAPI prediction-API provider.

MUAPI is not OpenAI-compatible: each model is its own endpoint that takes a
single prompt, and long requests are polled at `/predictions/{id}/result`.
This module is kept for users who already have a MUAPI key. New setups use
`openai_compatible_service` instead.
"""

import json
import asyncio
import httpx
from typing import AsyncGenerator, Dict, Any, List, Optional

from app.schemas.contracts import ModelInfo
from app.services.llm_config import LLMConfig, resolve_llm_config
from app.services.storage_service import storage_service


# Text-generation models exposed by the MUAPI registry. MUAPI has no model
# listing endpoint, so this stays static.
MUAPI_MODELS: List[ModelInfo] = [
    # Grok Family
    ModelInfo(id="grok-4-5", name="Grok 4.5", provider="xAI / MUAPI", description="High-speed reasoning & deep analysis engine", recommended=True),
    ModelInfo(id="grok-4-3", name="Grok 4.3", provider="xAI / MUAPI", description="High-performance xAI reasoning model"),
    ModelInfo(id="grok-4-6", name="Grok 4.6", provider="xAI / MUAPI", description="Next-gen xAI reasoning tier"),
    ModelInfo(id="grok-4-7", name="Grok 4.7", provider="xAI / MUAPI", description="Flagship xAI intelligence engine"),

    # Claude Family
    ModelInfo(id="claude-sonnet-4-5", name="Claude 4.5 Sonnet", provider="Anthropic / MUAPI", description="Advanced code generation & refactoring agent"),
    ModelInfo(id="claude-sonnet-4-6", name="Claude 4.6 Sonnet", provider="Anthropic / MUAPI", description="Flagship Anthropic model for enterprise workflows"),
    ModelInfo(id="claude-sonnet-5", name="Claude 5 Sonnet", provider="Anthropic / MUAPI", description="Next-generation Claude reasoning engine"),
    ModelInfo(id="claude-opus-4-5", name="Claude 4.5 Opus", provider="Anthropic / MUAPI", description="Ultra-complex reasoning & multi-turn problem solving"),
    ModelInfo(id="claude-opus-4-6", name="Claude 4.6 Opus", provider="Anthropic / MUAPI", description="Flagship Opus deep intelligence engine"),
    ModelInfo(id="claude-opus-4-7", name="Claude 4.7 Opus", provider="Anthropic / MUAPI", description="Extended reasoning Opus model"),
    ModelInfo(id="claude-opus-4-8", name="Claude 4.8 Opus", provider="Anthropic / MUAPI", description="Max-capacity Opus tier"),
    ModelInfo(id="claude-opus-5", name="Claude 5 Opus", provider="Anthropic / MUAPI", description="Next-gen flagship Opus intelligence"),
    ModelInfo(id="claude-haiku-4-5", name="Claude 4.5 Haiku", provider="Anthropic / MUAPI", description="Fast lightweight Anthropic model"),
    ModelInfo(id="claude-fable-5", name="Claude 5 Fable", provider="Anthropic / MUAPI", description="Creative & story generation specialist"),

    # Gemini Family
    ModelInfo(id="gemini-2-5-pro", name="Gemini 2.5 Pro", provider="Google / MUAPI", description="Deep context window & multimodal reasoning"),
    ModelInfo(id="gemini-2-5-flash", name="Gemini 2.5 Flash", provider="Google / MUAPI", description="Ultra-fast lightweight Google model"),
    ModelInfo(id="gemini-3-flash", name="Gemini 3 Flash", provider="Google / MUAPI", description="Next-gen Gemini 3 high-efficiency model"),
    ModelInfo(id="gemini-3-5-flash", name="Gemini 3.5 Flash", provider="Google / MUAPI", description="Enhanced speed & accuracy Gemini tier"),
    ModelInfo(id="gemini-3-5-flash-openai", name="Gemini 3.5 Flash (OpenAI Format)", provider="Google / MUAPI", description="Gemini 3.5 Flash with OpenAI schema compatibility"),
    ModelInfo(id="gemini-3-6-flash", name="Gemini 3.6 Flash", provider="Google / MUAPI", description="Latest 3.6 Flash iteration"),
    ModelInfo(id="gemini-3-6-flash-openai", name="Gemini 3.6 Flash (OpenAI Format)", provider="Google / MUAPI", description="Gemini 3.6 Flash OpenAI format"),
    ModelInfo(id="gemini-3-1-pro", name="Gemini 3.1 Pro", provider="Google / MUAPI", description="Pro-grade reasoning & structured output engine"),
    ModelInfo(id="gemini-3-pro", name="Gemini 3 Pro", provider="Google / MUAPI", description="Flagship Gemini 3 reasoning engine"),

    # GPT Family
    ModelInfo(id="gpt-5-mini", name="GPT 5 Mini", provider="OpenAI / MUAPI", description="Compact high-speed GPT-5 model"),
    ModelInfo(id="gpt-5-nano", name="GPT 5 Nano", provider="OpenAI / MUAPI", description="Micro lightweight GPT-5 tier"),
    ModelInfo(id="gpt-5-2", name="GPT 5.2", provider="OpenAI / MUAPI", description="Next-gen GPT 5.2 intelligence tier"),
    ModelInfo(id="gpt-5-4", name="GPT 5.4", provider="OpenAI / MUAPI", description="Advanced GPT 5.4 model"),
    ModelInfo(id="gpt-5-5", name="GPT 5.5", provider="OpenAI / MUAPI", description="Flagship GPT 5.5 reasoning model"),
    ModelInfo(id="gpt-5-6-luna", name="GPT 5.6 Luna", provider="OpenAI / MUAPI", description="Specialized Luna variant of GPT 5.6"),
    ModelInfo(id="gpt-5-6-sol", name="GPT 5.6 Sol", provider="OpenAI / MUAPI", description="High-throughput Sol variant of GPT 5.6"),
    ModelInfo(id="gpt-5-6-terra", name="GPT 5.6 Terra", provider="OpenAI / MUAPI", description="Deep analysis Terra variant of GPT 5.6"),
    ModelInfo(id="gpt-codex", name="GPT Codex", provider="OpenAI / MUAPI", description="Code generation & refactoring specialist"),

    # DeepSeek & Kimi Family
    ModelInfo(id="deepseek-v4-pro", name="DeepSeek V4 Pro", provider="DeepSeek / MUAPI", description="Deep reasoning & mathematical intelligence engine"),
    ModelInfo(id="deepseek-v4-flash", name="DeepSeek V4 Flash", provider="DeepSeek / MUAPI", description="Fast open-weights deep reasoning engine"),
    ModelInfo(id="kimi-k3", name="Kimi K3", provider="Moonshot / MUAPI", description="Long-context Chinese & English reasoning model"),
]


class MuapiService:
    def __init__(self):
        pass

    async def stream_chat_completion(
        self,
        model: str,
        messages: List[Dict[str, str]],
        system_prompt: str = "",
        config: Optional[LLMConfig] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """
        Stream chat completions from MUAPI API endpoint. `tools` is accepted
        for interface parity and ignored: MUAPI takes a single prompt.
        Uses exact user-selected model slug without any model remapping or fallback.
        """
        config = config or resolve_llm_config(storage_service.get_app_settings())
        api_key = config.api_key
        base_url = config.base_url

        if not api_key:
            yield {
                "type": "content.delta",
                "delta": "Error: MUAPI API Key is missing. Please configure your key in App Settings & API Credentials."
            }
            yield {"type": "turn.completed", "ok": False}
            return

        # Target EXACT model slug passed by user without any alias remapping or fallback
        target_endpoint = (model or config.default_model).strip()

        # Extract latest user prompt and image_url
        user_prompt = ""
        image_url = None
        for m in reversed(messages):
            if m.get("role") == "user":
                if not user_prompt:
                    user_prompt = m.get("content", "")
                if not image_url and m.get("image_url"):
                    image_url = m.get("image_url")
                break

        # Format last 10 previous conversation messages into system_prompt so MUAPI maintains memory
        previous_messages = messages[:-1] if len(messages) > 1 else []
        last_10_messages = previous_messages[-10:] if len(previous_messages) > 10 else previous_messages

        history_blocks = []
        for m in last_10_messages:
            role_name = "User" if m.get("role") == "user" else "Assistant"
            content = m.get("content", "").strip()
            msg_img = m.get("image_url")
            if msg_img:
                content = f"{content} [Attached Image: {msg_img}]".strip()
            if content:
                history_blocks.append(f"{role_name}: {content}")

        formatted_history = "\n".join(history_blocks)

        full_system_prompt = system_prompt.strip() if system_prompt else ""
        if formatted_history:
            context_prefix = f"### Recent Conversation History (Last {len(last_10_messages)} Messages):\n{formatted_history}\n\n### Instructions:\nRespond to the latest user prompt keeping the conversation context above in mind."
            if full_system_prompt:
                full_system_prompt = f"{full_system_prompt}\n\n{context_prefix}"
            else:
                full_system_prompt = context_prefix

        # Standard MUAPI headers matching muapiapp protocol
        headers = {
            "Content-Type": "application/json",
            "x-api-key": api_key,
        }

        # Format input payload matching MUAPI's exact image_url input schema
        input_body = {
            "prompt": user_prompt,
            "image_url": image_url if image_url else None,
            "system_prompt": full_system_prompt if full_system_prompt else None,
            "reasoning_effort": config.reasoning_effort or "low",
            "web_search": False
        }

        # Direct model endpoint submission URL (POST {base_url}/{target_endpoint})
        request_attempts = [
            (f"{base_url}/{target_endpoint}", input_body)
        ]

        last_error = ""

        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                for endpoint_url, body_data in request_attempts:
                    try:
                        response = await client.post(endpoint_url, json=body_data, headers=headers)

                        if response.status_code == 200:
                            res_data = response.json()

                            # Check for Task Prediction Submission (returns request_id)
                            request_id = res_data.get("request_id") or res_data.get("id")
                            if request_id:
                                # Async Polling Loop for predictions result (GET /predictions/{request_id}/result)
                                poll_url = f"{base_url}/predictions/{request_id}/result"
                                poll_deadline = asyncio.get_event_loop().time() + 180

                                while asyncio.get_event_loop().time() < poll_deadline:
                                    try:
                                        poll_res = await client.get(poll_url, headers=headers)
                                        if poll_res.status_code == 200:
                                            poll_data = poll_res.json()
                                            status = poll_data.get("status")
                                            error_text = poll_data.get("error")

                                            if status == "completed":
                                                # Safely parse outputs array (handles strings like ["Hi there! 😊"] or objects)
                                                output_text = ""
                                                outputs = poll_data.get("outputs", [])
                                                if isinstance(outputs, list) and len(outputs) > 0:
                                                    first_out = outputs[0]
                                                    if isinstance(first_out, str):
                                                        output_text = first_out
                                                    elif isinstance(first_out, dict):
                                                        output_text = first_out.get("text") or first_out.get("url") or str(first_out)

                                                if not output_text:
                                                    output_text = poll_data.get("result") or poll_data.get("output") or "Task completed."

                                                # Stream the real response text back to the frontend UI
                                                words = str(output_text).split(" ")
                                                for i, w in enumerate(words):
                                                    yield {"type": "content.delta", "delta": w + (" " if i < len(words) - 1 else "")}
                                                    await asyncio.sleep(0.02)
                                                yield {"type": "turn.completed", "ok": True}
                                                return

                                            elif status in ["failed", "cancelled", "error"] or error_text:
                                                err_msg = error_text or f"Task ended with status: {status}"
                                                yield {"type": "content.delta", "delta": f"Error: {err_msg}"}
                                                yield {"type": "turn.completed", "ok": False}
                                                return
                                        else:
                                            # Catch non-200 polling HTTP errors immediately (e.g. 404, 401, 500)
                                            poll_err = f"Error polling prediction result ({poll_url}) - HTTP {poll_res.status_code}: {poll_res.text}"
                                            yield {"type": "content.delta", "delta": f"Error: {poll_err}"}
                                            yield {"type": "turn.completed", "ok": False}
                                            return

                                    except Exception as poll_exc:
                                        yield {"type": "content.delta", "delta": f"Error during polling: {str(poll_exc)}"}
                                        yield {"type": "turn.completed", "ok": False}
                                        return

                                    await asyncio.sleep(0.5)


                            # Direct output in initial POST response
                            direct_output = ""
                            outputs = res_data.get("outputs", [])
                            if isinstance(outputs, list) and len(outputs) > 0:
                                first_out = outputs[0]
                                if isinstance(first_out, str):
                                    direct_output = first_out
                                elif isinstance(first_out, dict):
                                    direct_output = first_out.get("text") or first_out.get("url") or str(first_out)

                            if not direct_output:
                                direct_output = res_data.get("result") or res_data.get("output") or res_data.get("choices", [{}])[0].get("message", {}).get("content", "")

                            if direct_output:
                                words = str(direct_output).split(" ")
                                for i, w in enumerate(words):
                                    yield {"type": "content.delta", "delta": w + (" " if i < len(words) - 1 else "")}
                                    await asyncio.sleep(0.02)
                                yield {"type": "turn.completed", "ok": True}
                                return

                        else:
                            last_error = f"MUAPI Endpoint ({endpoint_url}) returned HTTP {response.status_code}: {response.text}"
                    except Exception as exc:
                        last_error = f"Connection error on ({endpoint_url}): {str(exc)}"

        except Exception as exc:
            last_error = f"MUAPI Client Error: {str(exc)}"

        # Output exact backend error message to frontend UI without any fallback mockup
        error_display = last_error if last_error else "Error: Unable to connect to MUAPI service."
        yield {"type": "content.delta", "delta": error_display}
        yield {"type": "turn.completed", "ok": False}

muapi_service = MuapiService()

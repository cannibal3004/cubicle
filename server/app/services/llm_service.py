"""Provider dispatch for chat streaming and the model catalog."""

from typing import Any, AsyncGenerator, Dict, List, Optional

import httpx

from app.schemas.contracts import ModelCatalog, ModelInfo
from app.services.llm_config import PROVIDER_MUAPI, LLMConfig, resolve_llm_config
from app.services.muapi_service import MUAPI_MODELS, muapi_service
from app.services.openai_compatible_service import openai_compatible_service
from app.services.identity import get_current_owner
from app.services.model_slots import model_slots
from app.services.storage_service import storage_service

# Model settings are app-wide; any owner's view carries them.


def current_llm_config() -> LLMConfig:
    return resolve_llm_config(storage_service.get_app_settings())


async def stream_chat_completion(
    model: str,
    messages: List[Dict[str, Any]],
    system_prompt: str = "",
    config: Optional[LLMConfig] = None,
    tools: Optional[List[Dict[str, Any]]] = None,
) -> AsyncGenerator[Dict[str, Any], None]:
    config = config or current_llm_config()
    if config.provider == PROVIDER_MUAPI:
        # MUAPI takes a single prompt; it cannot do function calling.
        generator = muapi_service.stream_chat_completion(model, messages, system_prompt, config)
    else:
        generator = openai_compatible_service.stream_chat_completion(
            model, messages, system_prompt, config, tools=tools
        )
    # One gate for every request: chat rounds, compaction summaries, all of it.
    async for event in model_slots.gated(get_current_owner(), generator):
        yield event


async def list_models(config: Optional[LLMConfig] = None, refresh: bool = False) -> ModelCatalog:
    config = config or current_llm_config()
    if config.provider == PROVIDER_MUAPI:
        models = [
            model.model_copy(update={"recommended": model.id == config.default_model})
            for model in MUAPI_MODELS
        ]
        return ModelCatalog(
            provider=config.provider, base_url=config.base_url, source="static", models=models
        )

    try:
        models = await openai_compatible_service.list_models(config, refresh=refresh)
    except (httpx.HTTPError, ValueError) as exc:
        # Keep the UI usable: the configured default model stays selectable and
        # the picker also accepts a typed model ID.
        fallback = ModelInfo(
            id=config.default_model,
            name=config.default_model,
            provider="Configured default",
            description="The server did not return a model list.",
            recommended=True,
            is_available=False,
        )
        return ModelCatalog(
            provider=config.provider,
            base_url=config.base_url,
            source="fallback",
            error=str(exc),
            models=[fallback],
        )
    return ModelCatalog(
        provider=config.provider, base_url=config.base_url, source="remote", models=models
    )

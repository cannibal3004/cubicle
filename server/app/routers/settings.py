import time
from typing import Any, Dict, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.schemas.contracts import AppSettingsSchema
from app.services.identity import is_admin
from app.services.storage_service import APP_SETTING_KEYS, current_storage

router = APIRouter(prefix="/api/v1/settings", tags=["settings"])


@router.get("", response_model=AppSettingsSchema)
async def get_settings():
    # App-wide values overlaid with the caller's personal ones.
    data = current_storage().get_public_settings()
    return AppSettingsSchema(**data)


@router.post("", response_model=AppSettingsSchema)
async def save_settings(new_settings: AppSettingsSchema, request: Request):
    # Only the keys the client sent change; a page that knows nothing about a
    # newer setting must not reset it to the default.
    changes = new_settings.model_dump(exclude_unset=True)
    app_keys = sorted(APP_SETTING_KEYS.intersection(changes))
    if app_keys and not is_admin(getattr(request.state, "user", None)):
        raise HTTPException(status_code=403, detail=f"Only an admin can change app-wide settings ({', '.join(app_keys)}).")
    current_storage().save_settings(changes)
    if {"auto_approval", "decider_threshold"} & set(changes):
        # Bots that follow the app's auto-approval may have a card waiting.
        from app.routers.chat import schedule_recheck

        schedule_recheck()
    return AppSettingsSchema(**current_storage().get_public_settings())


class WebSearchTest(BaseModel):
    # app: everyone's provider (admins); account: the caller's own. Values left empty are the saved ones.
    scope: Literal["app", "account"] = "app"
    provider: Literal["", "exa", "tavily", "searxng"] = ""
    api_key: str = Field(default="", max_length=500)
    url: str = Field(default="", max_length=300)


@router.post("/web-search/test")
async def test_web_search(body: WebSearchTest, request: Request) -> Dict[str, Any]:
    """A two-result search with the given provider, key and address (or the saved ones), to check them before bots rely on them."""
    from app.services import web_search

    if body.scope == "app" and not is_admin(getattr(request.state, "user", None)):
        raise HTTPException(status_code=403, detail="Only an admin can test the app-wide web search.")
    saved = current_storage().get_settings()
    prefix = "web_search_" if body.scope == "app" else "user_web_search_"
    provider = body.provider or str(saved.get(f"{prefix}provider") or "")
    config = web_search.WebConfig(
        provider=provider,
        api_key=body.api_key or str(saved.get(f"{prefix}api_key") or ""),
        url=(body.url or str(saved.get(f"{prefix}url") or "")).rstrip("/"),
    )
    if provider not in web_search.PROVIDERS:
        return {"ok": False, "error": "Choose a provider first."}
    if not web_search._usable(config.provider, config.api_key, config.url):
        return {"ok": False, "error": "SearXNG needs its address." if provider == "searxng" else "This provider needs an API key."}
    started = time.monotonic()
    try:
        found = await web_search.search(config, query="Cubicle self-hosted AI bots", max_results=2)
    except web_search.WebSearchError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "provider": found["provider"], "results": len(found["results"]), "first": (found["results"] or [{}])[0].get("title", ""),
            "ms": round((time.monotonic() - started) * 1000), **{k: found[k] for k in ("cost_usd", "credits") if k in found}}

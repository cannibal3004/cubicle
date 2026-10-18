from fastapi import APIRouter, HTTPException, Request
from typing import Dict, Any
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
    return AppSettingsSchema(**current_storage().get_public_settings())

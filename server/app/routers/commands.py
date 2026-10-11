"""The slash-command registry, for the composer's `/` picker."""

from fastapi import APIRouter

from app.services.commands import list_commands

router = APIRouter(prefix="/api/v1/commands", tags=["commands"])


@router.get("")
async def get_commands():
    return {"commands": list_commands()}

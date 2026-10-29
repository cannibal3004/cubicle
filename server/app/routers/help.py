"""Cubicle's help pages for the Help view (services/help.py)."""

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from app.services import help as help_service

router = APIRouter(prefix="/api/v1/help", tags=["help"])


@router.get("")
async def help_index():
    """The pages, in groups, as the index lists them."""
    try:
        return {"groups": help_service.index()}
    except help_service.HelpError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/pages/{path:path}")
async def help_page(path: str):
    try:
        return help_service.page(path)
    except help_service.HelpError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/images/{name}")
async def help_image(name: str):
    try:
        return FileResponse(help_service.image(name), headers={"Cache-Control": "private, max-age=3600"})
    except help_service.HelpError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/search")
async def help_search(q: str = Query("", max_length=1000), limit: int = Query(6, ge=1, le=20)):
    found = await help_service.search(q, limit)
    return {"results": [{"page": s.page, "title": s.title, "heading": s.heading, "link": s.link, "text": s.text[:400]} for s in found]}

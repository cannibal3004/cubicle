"""Files a bot has shared into the chat, and the folders the user can browse.

Two roots: the bot's computer workspace (bind mode) and the shared folder.
A bot id is only accepted for a bot of the signed-in account.
"""

from typing import List, Optional

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse

from app.services import file_browser, sharing
from app.services.file_share import FileShareError, normalize_source, resolve_shared_file
from app.services.storage_service import current_storage

router = APIRouter(prefix="/api/v1/files", tags=["files"])


def _own_bot(bot_id: str) -> Optional[str]:
    """The bot id when it is one of the caller's bots; None when none was given."""
    bot_id = (bot_id or "").strip()
    if not bot_id:
        return None
    sharing.enter_bot(bot_id)  # a bot shared with them: its owner's files
    if not any(bot.get("id") == bot_id for bot in current_storage().get_bots()):
        raise HTTPException(status_code=404, detail="No such bot.")
    return bot_id


@router.get("/download")
async def download_file(
    source: str = Query(...),
    path: str = Query(...),
    bot_id: str = Query(""),
    inline: bool = Query(False),
):
    """Download (or, with inline=1, preview) a shared or browsed file.

    The same confinement rules as sharing apply, so a stale card for a file
    that has moved outside the root, or been deleted, gets a 404.
    """
    try:
        shared = resolve_shared_file(source, path, _own_bot(bot_id))
    except FileShareError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return FileResponse(
        shared.absolute,
        media_type=shared.mime,
        filename=shared.name,
        content_disposition_type="inline" if inline else "attachment",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/list")
async def list_files(source: str = Query("computer"), path: str = Query(""), bot_id: str = Query("")):
    """One folder of the computer's workspace or the shared folder."""
    try:
        return file_browser.list_directory(source, path, _own_bot(bot_id))
    except FileShareError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/upload")
async def upload_files(
    source: str = Form("computer"),
    path: str = Form(""),
    bot_id: str = Form(""),
    files: List[UploadFile] = File(...),
):
    """Put files into a folder. Nothing is told to the bot; the files are simply there."""
    payload = [(upload.filename or "file", await upload.read()) for upload in files]
    try:
        stored = file_browser.store_files(source, path, _own_bot(bot_id), payload)
    except FileShareError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"stored": stored, "source": normalize_source(source), "path": path}


@router.post("/folder")
async def create_folder(source: str = Form("computer"), path: str = Form(""), bot_id: str = Form(""), name: str = Form(...)):
    try:
        return file_browser.make_directory(source, path, _own_bot(bot_id), name)
    except FileShareError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("")
async def delete_file(source: str = Query(...), path: str = Query(...), bot_id: str = Query("")):
    """Remove one file or one empty folder."""
    try:
        return file_browser.delete_entry(source, path, _own_bot(bot_id))
    except FileShareError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

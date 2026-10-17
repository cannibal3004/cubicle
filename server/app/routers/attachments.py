"""Upload a file to attach to the next message."""

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from app.config import settings
from app.services import attachments

router = APIRouter(prefix="/api/v1/attachments", tags=["attachments"])


@router.post("")
async def upload_attachment(file: UploadFile = File(...), bot_id: str = Form("")):
    """Save a file where the bot can reach it and return its card. The
    message that carries it names it by source and path in `attachments`."""
    limit = settings.ATTACHMENT_MAX_BYTES
    chunks = []
    total = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise HTTPException(status_code=413, detail=f"The file is larger than the {limit // (1024 * 1024)} MB attachment limit.")
        chunks.append(chunk)
    try:
        record = attachments.save_upload(bot_id.strip() or None, file.filename or "", b"".join(chunks))
    except attachments.AttachmentError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"attachment": record}

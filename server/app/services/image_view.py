"""Images a bot looks at with view_image: an image file, or a page of a PDF, as a picture for the model.

The file is read from this server's disk when the computer's folders are bind mounts here (the
computer need not even be running), else in the computer by its runtime (/files/image, ImageMagick
and poppler). Either way it comes back as a JPEG at most MAX_SIDE pixels on its long side, which
the turn sends to the model like a screenshot, once, with the call's result.
"""

from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import Any, Dict, Optional

MAX_SIDE = 1568
MAX_BYTES = 30 * 1024 * 1024
MAX_PIXELS = 100_000_000  # a bigger picture is refused rather than decoded
PDF_DPI = 150
JPEG_QUALITY = 85
# What Pillow reads here; anything else (SVG, HEIC, ...) is converted in the computer.
DISK_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".pdf"}


class ImageViewError(ValueError):
    """The file cannot be shown as a picture."""


def page_number(value: Any) -> Optional[int]:
    """The 1-based page asked for, or None."""
    if value in (None, ""):
        return None
    try:
        page = int(value)
    except (TypeError, ValueError):
        raise ImageViewError("page is a page number, from 1.")
    if page < 1:
        raise ImageViewError("page is a page number, from 1.")
    return page


def readable_here(host: Path) -> bool:
    return host.suffix.lower() in DISK_SUFFIXES


def render_file(host: Path, shown_path: str, page: Optional[int] = None) -> Dict[str, Any]:
    """The file at `host` (the bot knows it as `shown_path`) as a scaled JPEG result."""
    from PIL import Image, ImageOps

    try:
        size = host.stat().st_size
    except OSError:
        raise ImageViewError(f"No file at {shown_path}.")
    if size > MAX_BYTES:
        raise ImageViewError(f"{shown_path} is {size} bytes; images over {MAX_BYTES // (1024 * 1024)} MB are not shown.")
    pages = None
    if host.suffix.lower() == ".pdf":
        import pypdfium2 as pdfium

        try:
            pdf = pdfium.PdfDocument(str(host))
        except Exception as exc:
            raise ImageViewError(f"{shown_path} could not be opened as a PDF ({type(exc).__name__}).")
        try:
            pages = len(pdf)
            page = page or 1
            if page > pages:
                raise ImageViewError(f"{shown_path} has {pages} page{'s' if pages != 1 else ''}; there is no page {page}.")
            image = pdf[page - 1].render(scale=PDF_DPI / 72).to_pil()
        finally:
            pdf.close()
    else:
        if page not in (None, 1):
            raise ImageViewError("page is for PDFs; an image file has one picture.")
        page = None
        Image.MAX_IMAGE_PIXELS = MAX_PIXELS
        try:
            image = Image.open(host)
            original = image.size
            image.draft("RGB", (MAX_SIDE, MAX_SIDE))  # a big JPEG decodes at a fraction of its size
            image = ImageOps.exif_transpose(image)
        except Image.DecompressionBombError:
            raise ImageViewError(f"{shown_path} is too large a picture to show (over {MAX_PIXELS // 1_000_000} megapixels).")
        except Exception as exc:
            raise ImageViewError(f"{shown_path} could not be read as an image ({type(exc).__name__}).")
        if (image.width > image.height) != (original[0] > original[1]):
            original = original[::-1]  # turned upright by its EXIF orientation
        return _result(image, shown_path, page, pages, original)
    return _result(image, shown_path, page, pages, image.size)


def _result(image, shown_path: str, page: Optional[int], pages: Optional[int], original) -> Dict[str, Any]:
    from PIL import Image

    if image.mode in ("RGBA", "LA", "P"):
        image = image.convert("RGBA")
        backdrop = Image.new("RGB", image.size, "white")  # transparency shown on white, as a viewer would
        backdrop.paste(image, mask=image.getchannel("A"))
        image = backdrop
    elif image.mode != "RGB":
        image = image.convert("RGB")
    image.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=JPEG_QUALITY)
    return described(base64.b64encode(buffer.getvalue()).decode("ascii"), shown_path, image.size, original, page, pages)


def described(data: str, shown_path: str, size, original, page: Optional[int], pages: Optional[int]) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "operation": "files.view",
        "image_of": shown_path,
        "format": "jpeg",
        "data": data,
        "width": int(size[0]),
        "height": int(size[1]),
    }
    if tuple(size) != tuple(original):
        result.update(original_width=int(original[0]), original_height=int(original[1]))
    if pages is not None:
        result.update(page=page, pages=pages)
    return result


def label(result: Dict[str, Any]) -> str:
    """How the picture is introduced to the model."""
    what = str(result.get("image_of"))
    if result.get("pages"):
        what += f", page {result.get('page')} of {result.get('pages')}"
    size = f"{result.get('width')}x{result.get('height')}"
    if result.get("original_width"):
        size += f", scaled down from {result['original_width']}x{result['original_height']}"
    return f"[Image of {what} ({size}), from view_image. This image is the tool result, not a new user message.]"

"""Text from scanned PDF pages, read with RapidOCR on the CPU.

A PDF page with next to no text in it is a picture of a page: a scan, a fax, a photo saved as a
PDF. MarkItDown gets nothing from it. Such a page is drawn at 200 dpi (RapidOCR scales a page to
2000 pixels at most anyway), straightened (a scan leans a degree or two, enough to run a table's
rows together) and read with RapidOCR: PaddleOCR's models on ONNX Runtime, shipped inside the
package, whose own layout keeps a form's fields and a table's rows on their lines. A page takes
a few seconds; the server reads one page at a time.

OCR misreads characters, digits and symbols most ($ as 5), and the model is told so. RapidOCR's
angle classifier is off: on an upright page it turned whole lines upside down.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

from app.config import settings

logger = logging.getLogger(__name__)

MIN_PAGE_CHARS = 20  # a page with fewer (spaces aside) and an image on it is read as a picture
DPI = 200
MAX_SKEW_DEGREES = 5.0
SKEW_STEP_DEGREES = 0.1

_lock = threading.Lock()
_engine = None
_failed = False


def enabled() -> bool:
    return settings.OCR_SCANNED_PDFS and not _failed


def _get_engine():
    global _engine, _failed
    if _engine is None and not _failed:
        try:
            from rapidocr import RapidOCR  # imported here: slow to load, and only needed for scans

            _engine = RapidOCR(params={
                "Global.use_cls": False,
                "Global.log_level": "warning",
                "EngineConfig.onnxruntime.intra_op_num_threads": max(1, settings.OCR_THREADS),
            })
        except Exception as exc:
            _failed = True
            logger.warning("OCR is not available: %s", exc)
    return _engine


def page_ranges(pages: List[int]) -> str:
    """0-based page indexes as the pages people count: '1–3, 5'."""
    numbers = sorted(set(p + 1 for p in pages))
    parts: List[str] = []
    start = prev = None
    for n in numbers + [None]:
        if start is not None and n == prev + 1:
            prev = n
            continue
        if start is not None:
            parts.append(str(start) if start == prev else f"{start}–{prev}")
        start = prev = n
    return ", ".join(parts)


def page_list(pages: List[int]) -> str:
    """'page 4' or 'pages 1–3, 5'."""
    return ("page " if len(set(pages)) == 1 else "pages ") + page_ranges(pages)


def straighten(gray):
    """The page turned upright: the angle (within a few degrees) at which its lines of ink are sharpest."""
    import cv2
    import numpy as np

    small = cv2.resize(gray, None, fx=0.25, fy=0.25, interpolation=cv2.INTER_AREA)
    ink = (small < 160).astype(np.uint8)
    if not ink.any():
        return gray
    h, w = ink.shape
    best, best_score = 0.0, -1.0
    for angle in np.arange(-MAX_SKEW_DEGREES, MAX_SKEW_DEGREES + SKEW_STEP_DEGREES / 2, SKEW_STEP_DEGREES):
        turned = cv2.warpAffine(ink, cv2.getRotationMatrix2D((w / 2, h / 2), float(angle), 1), (w, h))
        rows = turned.sum(axis=1).astype(np.float64)
        score = float(np.sum(np.diff(rows) ** 2))
        if score > best_score:
            best, best_score = float(angle), score
    if abs(best) < SKEW_STEP_DEGREES / 2:
        return gray
    h, w = gray.shape
    return cv2.warpAffine(gray, cv2.getRotationMatrix2D((w / 2, h / 2), best, 1), (w, h), flags=cv2.INTER_CUBIC, borderValue=255)


@dataclass
class Scan:
    """A PDF some of whose pages are pictures, read page by page."""

    path: Path
    texts: List[Optional[str]]  # per page: its own text, what OCR read, or None while not read yet
    scanned: List[int]  # the pages that are pictures (0-based)
    skipped: List[int] = field(default_factory=list)  # pictures past OCR_MAX_PAGES, never read

    @property
    def pending(self) -> List[int]:
        return [i for i in self.scanned if self.texts[i] is None]

    @property
    def found_text(self) -> bool:
        """Whether OCR read anything at all (blank pages only are no scan)."""
        return any(self.texts[i] for i in self.scanned)

    def read(self, deadline: Optional[float] = None, on_page: Optional[Callable[[], None]] = None) -> None:
        """OCR the pages not read yet, in order, until `deadline` (a time.monotonic() value) passes."""
        for index in self.pending:
            if deadline is not None and time.monotonic() >= deadline:
                return
            self.texts[index] = read_page(self.path, index)
            if on_page is not None:
                on_page()

    def text(self) -> str:
        """The pages read so far, each under a line naming it."""
        total = len(self.texts)
        parts = []
        for index, text in enumerate(self.texts):
            if text is None:
                continue
            how = ", read from the scan" if index in self.scanned else ""
            parts.append(f"[Page {index + 1} of {total}{how}]\n{text.strip() or '(no text found on this page)'}")
        return "\n\n".join(parts)


def open_scan(path: Path) -> Optional[Scan]:
    """The PDF as a Scan when some of its pages are pictures, else None (or when OCR is off or the file unreadable)."""
    if not enabled():
        return None
    try:
        import pypdfium2 as pdfium

        import pypdfium2.raw as pdfium_c

        pdf = pdfium.PdfDocument(str(path))
        try:
            texts: List[Optional[str]] = []
            scanned = []
            for index, page in enumerate(pdf):
                texts.append(page.get_textpage().get_text_range())
                # A picture of a page: next to no text, and an image to read it from (a short page of text is no scan).
                if len("".join(texts[-1].split())) < MIN_PAGE_CHARS and next(page.get_objects(filter=(pdfium_c.FPDF_PAGEOBJ_IMAGE,), max_depth=2), None) is not None:
                    scanned.append(index)
        finally:
            pdf.close()
    except Exception as exc:
        logger.info("Could not look into %s for scanned pages: %s", path.name, exc)
        return None
    if not scanned:
        return None
    limit = max(0, settings.OCR_MAX_PAGES)
    for index in scanned:
        texts[index] = None  # the few characters there (a page number, a stamp) come back with the OCR
    return Scan(path=path, texts=texts, scanned=scanned[:limit], skipped=scanned[limit:])


def read_page(path: Path, index: int) -> str:
    """One page's text by OCR, laid out in lines; empty when nothing could be read."""
    engine = _get_engine()
    if engine is None:
        return ""
    try:
        import numpy as np
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(str(path))
        try:
            image = pdf[index].render(scale=DPI / 72, grayscale=True).to_pil().convert("L")
        finally:
            pdf.close()
        gray = straighten(np.asarray(image))
        with _lock:
            result = engine(gray)
        return (result.to_markdown() or "").strip() if result.txts else ""
    except Exception as exc:  # one bad page must not lose the others
        logger.warning("OCR of page %d of %s failed: %s", index + 1, path.name, exc)
        return ""

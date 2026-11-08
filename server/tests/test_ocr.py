import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pypdfium2 as pdfium
from PIL import Image, ImageDraw, ImageFont

from app.routers import chat as chat_router
from app.services import attachments, ocr
from app.services.computer_provider import computer_id_for_bot
from app.services.context_builder import build_history_messages

from tests.test_attachments import MINIMAL_PDF


def scanned_page(lines, angle=0.0):
    """A letter page at 150 dpi with these lines of text on it, as a scanner would see it."""
    page = Image.new("L", (1275, 1650), 255)
    draw = ImageDraw.Draw(page)
    font = ImageFont.load_default(size=44)
    for n, line in enumerate(lines):
        draw.text((120, 160 + n * 90), line, fill=0, font=font)
    return page.rotate(angle, resample=Image.BICUBIC, fillcolor=255) if angle else page


def write_scan(path: Path, pages):
    pages[0].save(path, "PDF", resolution=150, save_all=True, append_images=pages[1:])


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_page_ranges(self):
        self.assertEqual(ocr.page_ranges([0, 1, 2, 4, 6, 7]), "1–3, 5, 7–8")
        self.assertEqual(ocr.page_ranges([3]), "4")
        self.assertEqual(ocr.page_ranges([]), "")
        self.assertEqual((ocr.page_list([3]), ocr.page_list([0, 1])), ("page 4", "pages 1–2"))

    def test_a_scanned_page_is_read_even_when_it_leans(self):
        path = self.root / "invoice.pdf"
        write_scan(path, [scanned_page(["INVOICE 4893", "Total due 357.00", "Net 20 days"], angle=1.8)])
        extract = attachments.extract_text(path)
        self.assertEqual(extract.note, "read from a scan with OCR")
        self.assertIn("[Page 1 of 1, read from the scan]", extract.text)
        for words in ("INVOICE 4893", "357.00", "Net 20 days"):
            self.assertIn(words, extract.text)
        self.assertTrue(extract.ocr.startswith("A scan, read with OCR (page 1). OCR misreads characters"))

    def test_only_the_pages_without_text_are_read(self):
        text_pdf = self.root / "text.pdf"
        text_pdf.write_bytes(MINIMAL_PDF)
        scan_pdf = self.root / "scan.pdf"
        write_scan(scan_pdf, [scanned_page(["Signed by the customer"])])
        mixed = pdfium.PdfDocument.new()
        mixed.import_pages(pdfium.PdfDocument(str(text_pdf)))
        mixed.import_pages(pdfium.PdfDocument(str(scan_pdf)))
        path = self.root / "mixed.pdf"
        mixed.save(str(path))
        scan = ocr.open_scan(path)
        self.assertEqual(scan.scanned, [1])
        extract = attachments.extract_text(path)
        self.assertEqual(extract.note, "the scanned pages read with OCR, the others from the PDF's own text")
        self.assertIn("[Page 1 of 2]\nQuarterly totals", extract.text)
        self.assertIn("[Page 2 of 2, read from the scan]", extract.text)
        self.assertIn("Signed by the customer", extract.text)

    def test_pdfs_with_text_and_blank_pages_keep_markitdown(self):
        path = self.root / "text.pdf"
        path.write_bytes(MINIMAL_PDF)
        self.assertIsNone(ocr.open_scan(path))
        self.assertEqual(attachments.extract_text(path).note, "converted to text with MarkItDown")
        blank = self.root / "blank.pdf"
        write_scan(blank, [scanned_page([])])
        extract = attachments.extract_text(blank)
        self.assertEqual(extract.ocr, "")
        self.assertNotIn("OCR", extract.note)

    def test_off_means_no_ocr(self):
        path = self.root / "invoice.pdf"
        write_scan(path, [scanned_page(["INVOICE 4893"])])
        with mock.patch.object(ocr.settings, "OCR_SCANNED_PDFS", False):
            self.assertIsNone(ocr.open_scan(path))
            self.assertNotIn("4893", attachments.extract_text(path).text)

    def test_pages_past_the_limit_are_not_read(self):
        path = self.root / "long.pdf"
        write_scan(path, [scanned_page([]) for _ in range(4)])
        with mock.patch.object(ocr.settings, "OCR_MAX_PAGES", 2):
            scan = ocr.open_scan(path)
        self.assertEqual((scan.scanned, scan.skipped), ([0, 1], [2, 3]))


class BackgroundScanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.computers = root / "computers"
        self.computers.mkdir()
        self.patches = [
            mock.patch.object(attachments.settings, "WORKSPACE_ROOT", root / "workspace"),
            mock.patch.object(attachments.settings, "COMPUTER_DOCKER_WORKSPACE_ROOT", self.computers),
            mock.patch.object(attachments.settings, "COMPUTER_PROVIDER", "docker"),
            mock.patch.object(attachments.settings, "COMPUTER_DOCKER_WORKSPACE_MODE", "bind"),
            mock.patch.object(attachments.settings, "ATTACHMENT_INLINE_MAX_CHARS", 10_000),
        ]
        for p in self.patches:
            p.start()
        pdf = root / "scan.pdf"
        write_scan(pdf, [scanned_page([]) for _ in range(3)])
        record = attachments.save_upload("bot-1", "Agreement.pdf", pdf.read_bytes())
        self.shared = attachments.resolve_shared_file("computer", record["path"], "bot-1")
        self.saved = self.computers / computer_id_for_bot("bot-1") / "uploads" / "Agreement.pdf.ocr.md"

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def attach(self, inline_seconds, posted, indexed):
        async def run():
            record = await attachments.attach(self.shared, "bot-1", "thread-1")
            await asyncio.gather(*attachments._scan_tasks)
            return record

        async def start_turn(thread_id, message, owner_id=None):
            posted.append((thread_id, message))

        with mock.patch.object(ocr, "read_page", lambda path, index: f"Clause {index + 1} text"), mock.patch.object(
            attachments.settings, "OCR_INLINE_SECONDS", inline_seconds
        ), mock.patch.object(chat_router, "start_scheduled_turn", start_turn), mock.patch.object(
            attachments, "schedule_indexing", lambda bot_id, record, text: indexed.append(text)
        ), mock.patch("app.services.conversations.get", return_value={"id": "thread-1"}):
            return asyncio.run(run())

    def test_a_scan_read_in_time_is_sent_with_the_message_and_saved(self):
        posted, indexed = [], []
        record = self.attach(30, posted, indexed)
        extract = record["extract"]
        self.assertIn("Clause 3 text", extract["text"])
        self.assertIn("The text read is also saved at /workspace/uploads/Agreement.pdf.ocr.md.", extract["ocr"])
        self.assertIn("Clause 2 text", self.saved.read_text(encoding="utf-8"))
        self.assertEqual(posted, [])
        self.assertEqual(len(indexed), 1)

    def test_pages_not_read_in_time_go_on_in_the_background_and_the_conversation_hears(self):
        posted, indexed = [], []
        record = self.attach(0, posted, indexed)
        extract = record["extract"]
        self.assertEqual(extract["text"], "")
        self.assertIn("Still being read: pages 1–3; each is added to /workspace/uploads/Agreement.pdf.ocr.md as it is done", extract["ocr"])
        self.assertIn("(not all pages yet)", extract["note"])
        # Read to the end afterwards: saved, indexed, and the conversation told.
        saved = self.saved.read_text(encoding="utf-8")
        self.assertIn("Clause 3 text", saved)
        self.assertNotIn("still being read", saved)
        self.assertEqual(len(indexed), 1)
        self.assertIn("Clause 1 text", indexed[0])
        (thread_id, message), = posted
        self.assertEqual(thread_id, "thread-1")
        self.assertIn("[The scanned file Agreement.pdf has been read to the end.] It was read with OCR (pages 1–3,", message["text"])
        self.assertIn("All of it is in /workspace/uploads/Agreement.pdf.ocr.md.", message["text"])
        self.assertEqual(message["raw_payload"], {"scan_read": {"name": "Agreement.pdf", "pages": 3}})

    def test_the_model_sees_the_ocr_warning_before_the_text(self):
        message = {
            "sender": "user",
            "text": "What does it say?",
            "attachments": [{"name": "scan.pdf", "size": 900, "mime": "application/pdf", "location": "/workspace/uploads/scan.pdf",
                             "extract": {"text": "[Page 1 of 1, read from the scan]\nTotal 5357.00", "chars": 45, "truncated": False,
                                         "note": "read from a scan with OCR", "ocr": "A scan, read with OCR (page 1)."}}],
        }
        content = build_history_messages([message], replay_tools=True)[0]["content"]
        self.assertIn("work with the whole file.]\n(A scan, read with OCR (page 1).)\n--- scan.pdf (read from a scan with OCR;", content)


if __name__ == "__main__":
    unittest.main()

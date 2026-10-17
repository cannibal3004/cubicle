import asyncio
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from fastapi import HTTPException
from starlette.datastructures import UploadFile

from app.routers import attachments as attachments_router
from app.routers import chat as chat_router
from app.schemas.contracts import TurnRequest
from app.services import attachments, context_builder
from app.services.computer_provider import computer_id_for_bot
from app.services.context_builder import build_history_messages

CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'
)
RELS = (
    '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>'
)
MINIMAL_PDF = b"""%PDF-1.4
1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj
2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj
3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 300 144] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >> endobj
4 0 obj << /Length 46 >> stream
BT /F1 24 Tf 20 50 Td (Quarterly totals) Tj ET
endstream endobj
5 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj
trailer << /Root 1 0 R >>
%%EOF
"""


def write_docx(path: Path, paragraphs):
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("_rels/.rels", RELS)
        z.writestr(
            "word/document.xml",
            f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>{body}</w:body></w:document>',
        )


class AttachmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.workspace = root / "workspace"
        self.computers = root / "computers"
        self.workspace.mkdir()
        self.computers.mkdir()
        self.patches = [
            mock.patch.object(attachments.settings, "WORKSPACE_ROOT", self.workspace),
            mock.patch.object(attachments.settings, "COMPUTER_DOCKER_WORKSPACE_ROOT", self.computers),
            mock.patch.object(attachments.settings, "COMPUTER_PROVIDER", "docker"),
            mock.patch.object(attachments.settings, "COMPUTER_DOCKER_WORKSPACE_MODE", "bind"),
            mock.patch.object(attachments.settings, "COMPUTER_DOCKER_HOST_SHARED_DIR", "/srv/shared"),
            mock.patch.object(attachments.settings, "ATTACHMENT_MAX_BYTES", 64 * 1024),
            mock.patch.object(attachments.settings, "ATTACHMENT_INLINE_MAX_CHARS", 200),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    # ---- saving -----------------------------------------------------------

    def test_upload_lands_in_the_bots_computer_workspace(self):
        record = attachments.save_upload("bot-1", "Q3 report.txt", b"hello")
        self.assertEqual(record["source"], "computer")
        self.assertEqual(record["path"], "uploads/Q3 report.txt")
        self.assertEqual(record["location"], "/workspace/uploads/Q3 report.txt")
        self.assertEqual(record["size"], 5)
        self.assertEqual(record["mime"], "text/plain")
        self.assertEqual(attachments.save_upload("bot-1", "memo.docx", b"zip")["mime"], "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        self.assertTrue((self.computers / computer_id_for_bot("bot-1") / "uploads" / "Q3 report.txt").is_file())

    def test_upload_without_a_computer_goes_to_the_shared_folder(self):
        with mock.patch.object(attachments.settings, "COMPUTER_PROVIDER", "fake"):
            record = attachments.save_upload("bot-1", "notes.md", b"# hi")
        self.assertEqual(record["source"], "workspace")
        self.assertEqual(record["path"], "uploads/notes.md")
        self.assertTrue((self.workspace / "uploads" / "notes.md").is_file())
        # Where the model is told to look depends on whether computers mount the folder.
        with mock.patch.object(attachments.settings, "COMPUTER_PROVIDER", "fake"), mock.patch.object(attachments.settings, "COMPUTER_DOCKER_HOST_SHARED_DIR", ""):
            self.assertIn("shared_folder_read", attachments.location_for("workspace", "uploads/notes.md"))
        self.assertTrue(attachments.location_for("workspace", "uploads/notes.md").startswith("/shared/uploads/notes.md"))

    def test_names_are_sanitised_and_deduplicated(self):
        self.assertEqual(attachments.safe_filename("../../etc/passwd"), "passwd")
        self.assertEqual(attachments.safe_filename("C:\\Users\\me\\My  Report?.PDF"), "My Report.PDF")
        self.assertEqual(attachments.safe_filename("..."), "file")
        self.assertEqual(attachments.safe_filename(""), "file")
        long = attachments.safe_filename("a" * 300 + ".xlsx")
        self.assertTrue(long.endswith(".xlsx") and len(long) <= attachments.MAX_NAME_LENGTH)
        first = attachments.save_upload("bot-1", "data.csv", b"a,b\n")
        second = attachments.save_upload("bot-1", "data.csv", b"c,d\n")
        third = attachments.save_upload("bot-1", "data.csv", b"e,f\n")
        self.assertEqual([first["path"], second["path"], third["path"]], ["uploads/data.csv", "uploads/data (2).csv", "uploads/data (3).csv"])

    def test_empty_and_oversized_uploads_are_refused(self):
        with self.assertRaises(attachments.AttachmentError):
            attachments.save_upload("bot-1", "x.txt", b"")
        with self.assertRaises(attachments.AttachmentError):
            attachments.save_upload("bot-1", "x.txt", b"x" * (64 * 1024 + 1))

    def test_upload_route_streams_with_a_size_limit(self):
        upload = UploadFile(filename="big.bin", file=io.BytesIO(b"x" * (64 * 1024 + 1)))
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(attachments_router.upload_attachment(file=upload, bot_id="bot-1"))
        self.assertEqual(ctx.exception.status_code, 413)
        upload = UploadFile(filename="ok.txt", file=io.BytesIO(b"fine"))
        result = asyncio.run(attachments_router.upload_attachment(file=upload, bot_id="bot-1"))
        self.assertEqual(result["attachment"]["path"], "uploads/ok.txt")
        self.assertNotIn("extract", result["attachment"])

    # ---- text extraction ---------------------------------------------------

    def test_plain_text_is_read_and_capped(self):
        path = self.workspace / "notes.txt"
        path.write_bytes(b"line\n" * 100)
        extract = attachments.extract_text(path)
        self.assertTrue(extract.truncated)
        self.assertEqual(len(extract.text), 200)
        self.assertEqual(extract.chars, 499)  # stripped
        self.assertEqual(extract.note, "read as text")
        csv = self.workspace / "data.csv"
        csv.write_bytes(b"a,b\n1,2\n")
        self.assertEqual(attachments.extract_text(csv).text, "a,b\n1,2")

    def test_binary_and_images_have_no_text(self):
        blob = self.workspace / "blob.bin"
        blob.write_bytes(bytes(range(256)) * 4)
        extract = attachments.extract_text(blob)
        self.assertEqual(extract.text, "")
        self.assertIn("no text", extract.note)
        fake_text = self.workspace / "really-binary.txt"
        fake_text.write_bytes(b"\x00\x01\x02")
        self.assertEqual(attachments.extract_text(fake_text).note, "binary data; no text to show")
        image = self.workspace / "photo.png"
        image.write_bytes(b"\x89PNG")
        self.assertIn("image", attachments.extract_text(image).note)

    def test_office_and_pdf_files_are_converted(self):
        import openpyxl

        docx = self.workspace / "memo.docx"
        write_docx(docx, ["Budget memo for October", "Spend is under plan."])
        extract = attachments.extract_text(docx)
        self.assertIn("Budget memo for October", extract.text)
        self.assertIn("MarkItDown", extract.note)

        xlsx = self.workspace / "totals.xlsx"
        book = openpyxl.Workbook()
        sheet = book.active
        sheet.title = "Totals"
        sheet.append(["Region", "Units"])
        sheet.append(["North", 120])
        book.save(xlsx)
        text = attachments.extract_text(xlsx).text
        self.assertIn("| Region | Units |", text)
        self.assertIn("North", text)

        pdf = self.workspace / "report.pdf"
        pdf.write_bytes(MINIMAL_PDF)
        self.assertIn("Quarterly totals", attachments.extract_text(pdf).text)

        html = self.workspace / "page.html"
        html.write_text("<html><body><h1>Title</h1><p>Hello <b>world</b></p></body></html>", encoding="utf-8")
        self.assertEqual(attachments.extract_text(html).text, "# Title\n\nHello **world**")

    def test_corrupt_office_file_is_attached_without_text(self):
        broken = self.workspace / "broken.docx"
        broken.write_bytes(bytes(range(256)) * 4)
        extract = attachments.extract_text(broken)
        self.assertIn("no text could be extracted", extract.note)

    # ---- the message and the model -----------------------------------------

    def test_send_attaches_resolved_files_with_text_and_history_hides_it(self):
        attachments.save_upload("bot-1", "memo.txt", b"The quarterly memo.")
        stored = []
        request = TurnRequest(thread_id="bot-1", bot_id="bot-1", user_text="Summarise this", attachments=[{"source": "computer", "path": "uploads/memo.txt"}])
        with mock.patch.object(chat_router.storage_service, "add_message", side_effect=stored.append), mock.patch.object(
            chat_router.llm_service, "current_llm_config", return_value=mock.Mock(default_model="m")
        ):
            response = asyncio.run(chat_router.send_message(request))
        self.assertEqual(len(stored), 1)
        attachment = stored[0]["attachments"][0]
        self.assertEqual(attachment["extract"]["text"], "The quarterly memo.")
        self.assertEqual(attachment["location"], "/workspace/uploads/memo.txt")
        # The response and the history endpoint carry the card, not the text.
        self.assertNotIn("extract", response["message"]["attachments"][0])
        self.assertEqual(response["message"]["attachments"][0]["text_chars"], 19)
        with mock.patch.object(chat_router.storage_service, "get_messages", return_value=stored):
            history = asyncio.run(chat_router.get_history("bot-1"))
        self.assertNotIn("extract", history[0]["attachments"][0])
        self.assertIn("extract", stored[0]["attachments"][0])  # the stored copy is untouched

    def test_send_rejects_paths_outside_the_roots_and_too_many_files(self):
        request = TurnRequest(thread_id="bot-1", bot_id="bot-1", user_text="x", attachments=[{"source": "computer", "path": "../../etc/passwd"}])
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(chat_router.send_message(request))
        self.assertEqual(ctx.exception.status_code, 400)
        many = [{"source": "computer", "path": f"uploads/{i}.txt"} for i in range(attachments.MAX_PER_MESSAGE + 1)]
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(chat_router.send_message(TurnRequest(thread_id="bot-1", bot_id="bot-1", user_text="x", attachments=many)))
        self.assertIn("At most", ctx.exception.detail)

    def test_model_sees_the_header_and_text_after_the_users_words(self):
        message = {
            "sender": "user",
            "text": "What does it say?",
            "attachments": [
                {"name": "memo.txt", "size": 19, "mime": "text/plain", "location": "/workspace/uploads/memo.txt", "extract": {"text": "The quarterly memo.", "chars": 19, "truncated": False, "note": "read as text"}},
                {"name": "big.pdf", "size": 5_000_000, "mime": "application/pdf", "location": "/workspace/uploads/big.pdf", "extract": {"text": "Page one", "chars": 90_000, "truncated": True, "note": "converted to text with MarkItDown"}},
                {"name": "blob.bin", "size": 10, "mime": "application/octet-stream", "location": "/workspace/uploads/blob.bin", "extract": {"text": "", "chars": 0, "truncated": False, "note": "no text could be extracted (UnsupportedFormatException)"}},
            ],
        }
        content = build_history_messages([message], replay_tools=True)[0]["content"]
        self.assertTrue(content.startswith("What does it say?\n\n[Attached file: memo.txt (19 B, text/plain). Saved at /workspace/uploads/memo.txt;"))
        self.assertIn("--- memo.txt (read as text; 19 characters) ---\nThe quarterly memo.\n--- end of memo.txt ---", content)
        self.assertIn("showing the first 8 of 90,000 characters; the rest is only in the file", content)
        self.assertIn("4.8 MB", content)
        self.assertIn("(No text is shown: no text could be extracted (UnsupportedFormatException).)", content)
        # A message with files and no words is just the files.
        content = build_history_messages([{**message, "text": ""}], replay_tools=True)[0]["content"]
        self.assertTrue(content.startswith("[Attached file: memo.txt"))
        # Messages without attachments are unchanged.
        self.assertEqual(build_history_messages([{"sender": "user", "text": "plain"}], replay_tools=True)[0]["content"], "plain")

    def test_extraction_timeout_leaves_the_file_attached(self):
        record = attachments.save_upload("bot-1", "slow.txt", b"slow")
        shared = attachments.resolve_shared_file("computer", record["path"], "bot-1")

        def slow(_path, limit=None):
            import time

            time.sleep(0.5)
            return attachments.Extract("late", 4, False, "read as text")

        with mock.patch.object(attachments.settings, "ATTACHMENT_EXTRACT_TIMEOUT", 0.05), mock.patch.object(attachments, "extract_text", slow):
            attached = asyncio.run(attachments.attach(shared, "bot-1"))
        self.assertEqual(attached["extract"]["text"], "")
        self.assertIn("did not finish", attached["extract"]["note"])


if __name__ == "__main__":
    unittest.main()

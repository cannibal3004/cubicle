import base64
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from app.routers.chat import _screenshot_message
from app.services import file_tracking, image_view
from app.services.action_gateway import ActionGateway, ActionInvocation
from app.services.computer_actions import register_computer_actions
from app.services.computer_provider import FakeComputerProvider, computer_id_for_bot
from app.services.llm_tools import build_invocation
from app.services.tool_names import resolve
from app.services.workspace_service import WorkspaceService

from tests.test_computer_provider import ImmediateApprovalBroker, RecordingAudit


def decoded(result):
    return Image.open(io.BytesIO(base64.b64decode(result["data"])))


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_a_big_image_is_scaled_to_a_jpeg_and_described(self):
        path = self.root / "photo.png"
        Image.new("RGB", (4000, 2000), "red").save(path)
        result = image_view.render_file(path, "/workspace/photo.png")
        self.assertEqual((result["format"], result["width"], result["height"]), ("jpeg", 1568, 784))
        self.assertEqual((result["original_width"], result["original_height"]), (4000, 2000))
        self.assertEqual(decoded(result).size, (1568, 784))
        self.assertEqual(image_view.label(result), "[Image of /workspace/photo.png (1568x784, scaled down from 4000x2000), from view_image. "
                                                   "This image is the tool result, not a new user message.]")

    def test_transparency_is_shown_on_white_and_small_images_keep_their_size(self):
        path = self.root / "icon.png"
        Image.new("RGBA", (64, 32), (0, 0, 0, 0)).save(path)
        result = image_view.render_file(path, "/workspace/icon.png")
        self.assertEqual((result["width"], result["height"]), (64, 32))
        self.assertNotIn("original_width", result)
        self.assertGreater(min(decoded(result).getpixel((10, 10))), 240)

    def test_a_pdf_page_is_drawn(self):
        path = self.root / "two.pdf"
        pages = [Image.new("RGB", (850, 1100), color) for color in ("white", "blue")]
        pages[0].save(path, "PDF", resolution=100, save_all=True, append_images=pages[1:])
        result = image_view.render_file(path, "/workspace/two.pdf", 2)
        self.assertEqual((result["page"], result["pages"]), (2, 2))
        red, green, blue = decoded(result).getpixel((50, 50))
        self.assertGreater(blue, red + 100)
        self.assertIn("/workspace/two.pdf, page 2 of 2", image_view.label(result))
        with self.assertRaisesRegex(image_view.ImageViewError, "has 2 pages; there is no page 3"):
            image_view.render_file(path, "/workspace/two.pdf", 3)

    def test_bad_requests_are_explained(self):
        path = self.root / "photo.jpg"
        Image.new("RGB", (10, 10)).save(path)
        with self.assertRaisesRegex(image_view.ImageViewError, "page is for PDFs"):
            image_view.render_file(path, "/workspace/photo.jpg", 2)
        broken = self.root / "broken.png"
        broken.write_bytes(b"not a picture")
        with self.assertRaisesRegex(image_view.ImageViewError, "could not be read as an image"):
            image_view.render_file(broken, "/workspace/broken.png")
        with self.assertRaisesRegex(image_view.ImageViewError, "No file at /workspace/gone.png"):
            image_view.render_file(self.root / "gone.png", "/workspace/gone.png")
        with self.assertRaisesRegex(image_view.ImageViewError, "from 1"):
            image_view.page_number(0)
        self.assertEqual(image_view.page_number("3"), 3)


class ToolTests(unittest.TestCase):
    def test_the_call_and_its_habitual_names(self):
        call = build_invocation("view_image", {"path": "shots/kvm.jpg", "page": 2}, "bot-1")
        self.assertEqual(call.name, "computer.files_view")
        self.assertEqual((call.arguments["path"], call.arguments["page"]), ("/workspace/shots/kvm.jpg", 2))
        self.assertNotIn("page", build_invocation("view_image", {"path": "/workspace/a.png"}, "bot-1").arguments)
        self.assertEqual(resolve("read_image", ["view_image", "read_file"]), "view_image")

    def test_the_model_gets_the_picture_after_the_label(self):
        message = _screenshot_message({"image_of": "/workspace/a.png", "format": "jpeg", "data": "QUJD", "width": 10, "height": 5})
        text, picture = message["content"]
        self.assertTrue(text["text"].startswith("[Image of /workspace/a.png (10x5), from view_image."))
        self.assertEqual(picture["image_url"]["url"], "data:image/jpeg;base64,QUJD")


class ActionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.computers = root / "computers"
        self.gateway = ActionGateway(workspace=WorkspaceService(root), approvals=ImmediateApprovalBroker(), audit=RecordingAudit())
        self.provider = FakeComputerProvider()
        register_computer_actions(self.gateway, self.provider)

    def tearDown(self):
        self.temp.cleanup()

    async def view(self, path, **extra):
        call = ActionInvocation(name="computer.files_view", arguments={"bot_id": "bot-1", "path": path, **extra}, target={"bot_id": "bot-1"}, preview="Look")
        request, _ = self.gateway.open("computer:bot-1", "bot-1", call)
        return await self.gateway.execute(request)

    def bind_mode(self):
        return mock.patch.multiple(file_tracking.settings, COMPUTER_PROVIDER="docker", COMPUTER_DOCKER_WORKSPACE_MODE="bind",
                                   COMPUTER_DOCKER_WORKSPACE_ROOT=self.computers)

    async def test_read_from_disk_without_starting_the_computer(self):
        folder = self.computers / computer_id_for_bot("bot-1") / "kvm"
        folder.mkdir(parents=True)
        Image.new("RGB", (800, 600), "green").save(folder / "shot.jpg")
        with self.bind_mode():
            result = await self.view("kvm/shot.jpg")
        self.assertEqual(result.status, "completed")
        self.assertEqual((result.result["image_of"], result.result["width"]), ("/workspace/kvm/shot.jpg", 800))
        self.assertEqual(self.provider.get_or_create("bot-1").state, "stopped")

    async def test_outside_the_bind_mounts_the_computer_converts_it(self):
        seen = {}

        async def files_view(computer_id, path, page=None):
            seen.update(path=path, page=page)
            return {"operation": "files.view", "path": path, "data": "QUJD", "width": 600, "height": 400, "original_width": 600, "original_height": 400}

        with mock.patch.object(self.provider, "files_view", files_view):
            result = await self.view("/workspace/diagram.svg", page="1")
        self.assertEqual(result.status, "completed")
        self.assertEqual(seen, {"path": "/workspace/diagram.svg", "page": 1})
        self.assertEqual(result.result["image_of"], "/workspace/diagram.svg")
        self.assertEqual(result.result["format"], "jpeg")
        self.assertIn("(600x400), from view_image", image_view.label(result.result))  # not "scaled down" when it was not
        self.assertTrue(result.result.get("computer_auto_started"))

    async def test_a_type_pillow_cannot_read_goes_to_the_computer_even_in_bind_mode(self):
        folder = self.computers / computer_id_for_bot("bot-1")
        folder.mkdir(parents=True)
        (folder / "logo.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
        with self.bind_mode(), mock.patch.object(self.provider, "files_view", mock.AsyncMock(return_value={"path": "/workspace/logo.svg", "data": "QUJD"})) as converted:
            result = await self.view("/workspace/logo.svg")
        self.assertEqual(result.status, "completed")
        converted.assert_awaited_once()

    async def test_errors_come_back_as_failures(self):
        with self.bind_mode():
            (self.computers / computer_id_for_bot("bot-1")).mkdir(parents=True)
            (self.computers / computer_id_for_bot("bot-1") / "a.png").write_bytes(b"junk")
            result = await self.view("/workspace/a.png")
        self.assertEqual(result.status, "failed")
        self.assertIn("could not be read as an image", result.error)


if __name__ == "__main__":
    unittest.main()

"""Screenshots scaled for the model, with pointer events mapped back to the screen."""

import base64
import io
import unittest
from unittest import mock

from PIL import Image

from app.services import screenshots
from app.services.screenshots import configured_max_width, map_input, scale_for_model


def _frame(width, height):
    image = Image.new("RGB", (width, height), (10, 20, 30))
    out = io.BytesIO()
    image.save(out, format="JPEG")
    return {"data": base64.b64encode(out.getvalue()).decode("ascii"), "format": "jpeg", "width": width, "height": height, "frame_id": "f1"}


def _size(result):
    image = Image.open(io.BytesIO(base64.b64decode(result["data"])))
    return image.size


class ScreenshotScalingTests(unittest.TestCase):
    def tearDown(self):
        screenshots.forget("c1")

    def test_a_wide_frame_is_scaled_and_clicks_map_back(self):
        scaled = scale_for_model("c1", _frame(1920, 1200), max_width=1280)
        self.assertEqual((scaled["width"], scaled["height"], scaled["screen_width"], scaled["screen_height"]), (1280, 800, 1920, 1200))
        self.assertTrue(scaled["scaled"])
        self.assertEqual(_size(scaled), (1280, 800))
        self.assertEqual(scaled["frame_id"], "f1")
        # The model clicks at (640, 400) in its image: that is (960, 600) on the screen.
        self.assertEqual(map_input("c1", {"type": "click", "x": 640, "y": 400}), {"type": "click", "x": 960, "y": 600})
        self.assertEqual(map_input("c1", {"type": "type", "text": "hi"}), {"type": "type", "text": "hi"})
        self.assertEqual(map_input("other", {"type": "click", "x": 1, "y": 1}), {"type": "click", "x": 1, "y": 1})

    def test_full_resolution_and_small_frames_pass_through(self):
        frame = _frame(1920, 1200)
        self.assertIs(scale_for_model("c1", frame, max_width=0), frame)
        self.assertEqual(map_input("c1", {"type": "click", "x": 5, "y": 6}), {"type": "click", "x": 5, "y": 6})
        small = _frame(1024, 640)
        self.assertIs(scale_for_model("c1", small, max_width=1280), small)
        # A frame scaled earlier no longer maps once a full-size one has been taken.
        scale_for_model("c1", _frame(1920, 1200), max_width=960)
        self.assertEqual(map_input("c1", {"type": "click", "x": 480, "y": 300})["x"], 960)
        scale_for_model("c1", _frame(1920, 1200), max_width=0)
        self.assertEqual(map_input("c1", {"type": "click", "x": 480, "y": 300})["x"], 480)

    def test_bad_image_data_is_left_alone(self):
        frame = {"data": "not-an-image", "width": 1920, "height": 1200}
        self.assertIs(scale_for_model("c1", frame, max_width=1280), frame)

    def test_the_setting_comes_from_app_settings_then_the_environment(self):
        with mock.patch.object(screenshots.settings, "COMPUTER_SCREENSHOT_MAX_WIDTH", 1600):
            with mock.patch("app.services.storage_service.current_storage", side_effect=RuntimeError("no store")):
                self.assertEqual(configured_max_width(), 1600)
            store = mock.Mock()
            store.get_settings.return_value = {"computer_screenshot_max_width": "0"}
            with mock.patch("app.services.storage_service.current_storage", return_value=store):
                self.assertEqual(configured_max_width(), 0)
            store.get_settings.return_value = {"computer_screenshot_max_width": 99999}
            with mock.patch("app.services.storage_service.current_storage", return_value=store):
                self.assertEqual(configured_max_width(), 4096)


if __name__ == "__main__":
    unittest.main()

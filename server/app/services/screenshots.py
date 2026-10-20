"""Screenshots as the model sees them: scaled down when asked, coordinates mapped back.

The computer's screen can be large (1920x1200 by default) and the user
watches it at full size over VNC. The model does not need that many pixels
to find a button, and every pixel costs vision tokens and time, so the
screenshot handed to the model can be scaled to a maximum width (the app
setting `computer_screenshot_max_width`; 0 keeps the full resolution).

The model then thinks in the scaled image's pixels. The scale used for the
last screenshot of each computer is remembered here, and pointer events are
mapped back to screen pixels before they reach the computer.
"""

from __future__ import annotations

import base64
import io
import logging
from typing import Any, Dict, Optional, Tuple

from app.config import settings

logger = logging.getLogger(__name__)

SETTING_KEY = "computer_screenshot_max_width"
MAX_WIDTH_LIMIT = 4096

# computer id -> (screen width / image width, screen height / image height)
_scales: Dict[str, Tuple[float, float]] = {}


def configured_max_width() -> int:
    """The app setting, else the environment default; 0 means full resolution."""
    from app.services.storage_service import current_storage

    value: Any = None
    try:
        value = current_storage().get_settings().get(SETTING_KEY)
    except Exception:  # settings unavailable: use the environment
        value = None
    if value is None or value == "":
        value = settings.COMPUTER_SCREENSHOT_MAX_WIDTH
    try:
        return max(0, min(MAX_WIDTH_LIMIT, int(value)))
    except (TypeError, ValueError):
        return max(0, min(MAX_WIDTH_LIMIT, settings.COMPUTER_SCREENSHOT_MAX_WIDTH))


def scale_for_model(computer_id: str, result: Dict[str, Any], max_width: Optional[int] = None) -> Dict[str, Any]:
    """Return the screenshot result with its image scaled to `max_width` when wider.

    `width`/`height` become the image the model sees; `screen_width`/
    `screen_height` keep the real screen. The scale is remembered for the
    computer so later pointer events are mapped back.
    """
    limit = configured_max_width() if max_width is None else max(0, int(max_width))
    data = result.get("data")
    width = int(result.get("width") or 0)
    height = int(result.get("height") or 0)
    _scales.pop(computer_id, None)
    if not isinstance(data, str) or not data or limit <= 0 or width <= limit or width <= 0 or height <= 0:
        return result
    try:
        from PIL import Image

        raw = base64.b64decode(data.split(",", 1)[1] if data.startswith("data:") else data)
        image = Image.open(io.BytesIO(raw))
        image.load()
        new_width = limit
        new_height = max(1, round(height * limit / width))
        resized = image.convert("RGB").resize((new_width, new_height), Image.LANCZOS)
        out = io.BytesIO()
        resized.save(out, format="JPEG", quality=80, optimize=True)
    except Exception as exc:  # a scaling failure must never lose the screenshot
        logger.warning("Screenshot for %s not scaled: %s", computer_id, exc)
        return result
    _scales[computer_id] = (width / new_width, height / new_height)
    return {
        **result,
        "data": base64.b64encode(out.getvalue()).decode("ascii"),
        "format": "jpeg",
        "width": new_width,
        "height": new_height,
        "screen_width": width,
        "screen_height": height,
        "scaled": True,
    }


def map_input(computer_id: str, event: Dict[str, Any]) -> Dict[str, Any]:
    """Pointer coordinates from the model's last screenshot to screen pixels."""
    scale = _scales.get(computer_id)
    if not scale:
        return event
    sx, sy = scale
    mapped = dict(event)
    for key, factor in (("x", sx), ("y", sy)):
        value = mapped.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            mapped[key] = round(float(value) * factor)
    return mapped


def forget(computer_id: str) -> None:
    _scales.pop(computer_id, None)

"""Demonstrations: the user shows the bot how to do something in its browser.

The computer runtime records what the user does while they drive the
desktop (see runtime/recorder.mjs): one line per click, typed field,
selection, key or navigation, with the control it happened on, plus a
desktop frame from just before each click with the click point marked.
When the user hands the computer back, the recording is saved under the
bot's data and the hand-back message carries it: the steps go into the
message text the model reads, and the turn that answers it also gets the
keyframes as images, once. Later turns see the steps in the history and
the skill the bot wrote from them (see services/procedures).
"""

from __future__ import annotations

import base64
import io
import json
import logging
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import settings
from app.services import screenshots

logger = logging.getLogger(__name__)

# Keyframes the model is shown for one demonstration; evenly spread when
# there are more (every step is still in the text).
MAX_KEYFRAMES_TO_MODEL = 12
MAX_STEPS_IN_TEXT = 200
MARK_RADIUS = 22
_ID = re.compile(r"^rec-[0-9a-f]{8}$")

LEAD = (
    "[While recording, the user showed this on your computer's browser. What they did, in order, with the page it happened on. "
    "Study it, then write it up as a skill of your own in your own words: numbered steps, each with the page or URL and the exact label "
    "of the control, so you can do it yourself next time with your computer tools. Save it with skill_save under a short name that "
    "says what it achieves, tell the user the name and the steps, and ask only about what is unclear.]"
)


def recordings_dir(bot_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", str(bot_id))[:80] or "bot"
    return settings.DATA_DIR / "recordings" / safe


def new_id() -> str:
    return f"rec-{uuid.uuid4().hex[:8]}"


# ---- steps as text -----------------------------------------------------------------------


def _control(step: Dict[str, Any]) -> str:
    target = step.get("target") or {}
    role = str(target.get("role") or target.get("tag") or "element")
    name = str(target.get("name") or "").strip()
    text = f'the {role} "{name}"' if name else f"a {role}"
    if target.get("frame"):
        text += " (inside an embedded frame)"
    return text


def _short_url(url: str) -> str:
    url = str(url or "")
    return url if len(url) <= 160 else url[:157] + "…"


def describe_step(step: Dict[str, Any], previous_url: Optional[str] = None) -> str:
    """One step as a line of text; the page is named when it changed since the previous step."""
    kind = str(step.get("type") or "")
    url = str(step.get("url") or "")
    where = f" on {_short_url(url)}" if url and url != previous_url and kind not in {"navigate", "start", "newtab"} else ""
    target = step.get("target") or {}
    if kind == "start":
        return f"Started on {_short_url(url)}."
    if kind == "navigate":
        return f"The page became {_short_url(url)}."
    if kind == "newtab":
        return f"A new tab opened at {_short_url(url)}."
    if kind == "click":
        button = str(step.get("button") or "left")
        verb = "Right-clicked" if button == "right" else "Clicked"
        at = f" at ({step.get('x')}, {step.get('y')})" if step.get("x") is not None and not target.get("name") else ""
        return f"{verb} {_control(step)}{at}{where}."
    if kind == "type":
        if target.get("secret"):
            return f"Typed a password into {_control(step)}{where} (not recorded)."
        value = str(step.get("value") or "")
        return f'Typed "{value}" into {_control(step)}{where}.' if value else f"Cleared {_control(step)}{where}."
    if kind == "select":
        return f'Chose "{step.get("value") or ""}" in {_control(step)}{where}.'
    if kind == "check":
        if "radio" in str(target.get("role") or ""):
            return f"Selected {_control(step)}{where}."
        return f"{'Checked' if step.get('checked') else 'Unchecked'} {_control(step)}{where}."
    if kind == "file":
        return f'Chose the file "{step.get("value") or ""}" with {_control(step)}{where}.'
    if kind == "key":
        key = str(step.get("key") or "")
        focus = f" in {_control(step)}" if target.get("name") else ""
        return f"Pressed {key}{focus}{where}."
    return f"{kind or 'Did something'}{where}."


def render_steps(recording: Dict[str, Any]) -> str:
    """The steps as a numbered list."""
    lines: List[str] = []
    previous: Optional[str] = None
    for step in list(recording.get("steps") or [])[:MAX_STEPS_IN_TEXT]:
        lines.append(f"{len(lines) + 1}. {describe_step(step, previous)}")
        if step.get("url"):
            previous = str(step["url"])
    dropped = int(recording.get("dropped_steps") or 0)
    if dropped:
        lines.append(f"(and {dropped} more steps that were not kept)")
    return "\n".join(lines)


# ---- saving -----------------------------------------------------------------------------


def _mark_and_scale(data: str, x: Optional[int], y: Optional[int], max_width: int) -> Optional[Dict[str, Any]]:
    """The frame with the click point circled, scaled like the bot's screenshots. None when it cannot be read."""
    try:
        from PIL import Image, ImageDraw

        raw = base64.b64decode(data.split(",", 1)[1] if data.startswith("data:") else data)
        image = Image.open(io.BytesIO(raw)).convert("RGB")
        if x is not None and y is not None:
            draw = ImageDraw.Draw(image)
            for radius, colour, width in ((MARK_RADIUS + 3, (255, 255, 255), 3), (MARK_RADIUS, (220, 38, 38), 4)):
                draw.ellipse((x - radius, y - radius, x + radius, y + radius), outline=colour, width=width)
        width, height = image.size
        if max_width > 0 and width > max_width:
            new_height = max(1, round(height * max_width / width))
            image = image.resize((max_width, new_height), Image.LANCZOS)
        out = io.BytesIO()
        image.save(out, format="JPEG", quality=80, optimize=True)
        return {"bytes": out.getvalue(), "width": image.size[0], "height": image.size[1], "screen_width": width, "screen_height": height}
    except Exception as exc:
        logger.warning("Keyframe not kept: %s", exc)
        return None


def save_recording(bot_id: str, payload: Dict[str, Any], *, max_width: Optional[int] = None) -> Dict[str, Any]:
    """Store a runtime recording for a bot: steps as JSON, keyframes as JPEG files. Returns the record without image data."""
    recording_id = new_id()
    folder = recordings_dir(bot_id) / recording_id
    folder.mkdir(parents=True, exist_ok=True)
    limit = screenshots.configured_max_width() if max_width is None else max(0, int(max_width))
    steps = [dict(step) for step in (payload.get("steps") or []) if isinstance(step, dict)]
    by_number = {int(step.get("n") or 0): step for step in steps}
    keyframes: List[Dict[str, Any]] = []
    for frame in payload.get("keyframes") or []:
        if not isinstance(frame, dict) or not isinstance(frame.get("data"), str):
            continue
        step = by_number.get(int(frame.get("step") or 0)) if frame.get("step") is not None else None
        x = step.get("screen_x") if step else None
        y = step.get("screen_y") if step else None
        made = _mark_and_scale(frame["data"], x, y, limit)
        if not made:
            continue
        name = f"frame-{len(keyframes) + 1:03d}.jpg"
        (folder / name).write_bytes(made["bytes"])
        keyframes.append({
            "file": name,
            "step": int(step["n"]) if step else None,
            "width": made["width"],
            "height": made["height"],
            "screen_width": made["screen_width"],
            "screen_height": made["screen_height"],
        })
    for step in steps:
        step.pop("keyframe", None)
    record = {
        "id": recording_id,
        "bot_id": bot_id,
        "created_at": datetime.now().isoformat(),
        "started_at": str(payload.get("started_at") or ""),
        "duration_ms": int(payload.get("duration_ms") or 0),
        "screen": {"width": int(payload.get("width") or 0), "height": int(payload.get("height") or 0)},
        "steps": steps,
        "dropped_steps": int(payload.get("dropped_steps") or 0),
        "keyframes": keyframes,
    }
    (folder / "recording.json").write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    return record


def load_recording(bot_id: str, recording_id: str) -> Optional[Dict[str, Any]]:
    if not isinstance(recording_id, str) or not _ID.match(recording_id):
        return None
    path = recordings_dir(bot_id) / recording_id / "recording.json"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) and record.get("bot_id") == bot_id else None


def summary(record: Dict[str, Any]) -> Dict[str, Any]:
    """What a stored user message keeps about its demonstration (steps as text, no images)."""
    return {
        "id": record.get("id"),
        "steps": len(record.get("steps") or []),
        "keyframes": len(record.get("keyframes") or []),
        "duration_ms": int(record.get("duration_ms") or 0),
        "text": render_steps(record),
    }


# ---- for the model ------------------------------------------------------------------------


def _chosen_frames(record: Dict[str, Any], limit: int) -> List[Dict[str, Any]]:
    frames = list(record.get("keyframes") or [])
    if len(frames) <= limit:
        return frames
    final = [frames[-1]] if frames and frames[-1].get("step") is None else []
    body = frames[: len(frames) - len(final)]
    room = max(1, limit - len(final))
    picked = [body[round(i * (len(body) - 1) / max(1, room - 1))] for i in range(room)] if room < len(body) else body
    unique: List[Dict[str, Any]] = []
    for frame in picked + final:
        if frame not in unique:
            unique.append(frame)
    return unique


def keyframe_message(bot_id: str, demonstration: Dict[str, Any], *, limit: int = MAX_KEYFRAMES_TO_MODEL) -> Optional[Dict[str, Any]]:
    """One user message carrying the keyframes as images, each introduced by its step; None when there are none."""
    record = load_recording(bot_id, str(demonstration.get("id") or ""))
    if not record:
        return None
    frames = _chosen_frames(record, limit)
    if not frames:
        return None
    by_number = {int(step.get("n") or 0): step for step in record.get("steps") or []}
    folder = recordings_dir(bot_id) / record["id"]
    parts: List[Dict[str, Any]] = [{
        "type": "text",
        "text": (
            f"[Screens from the demonstration above ({len(frames)} of {len(record.get('keyframes') or [])} frames). Each shows the screen "
            "just before a step, with the click point circled in red. They are part of the demonstration, not a new request.]"
        ),
    }]
    shown = 0
    for frame in frames:
        try:
            data = (folder / str(frame.get("file"))).read_bytes()
        except OSError:
            continue
        step = by_number.get(int(frame["step"])) if frame.get("step") is not None else None
        if step:
            caption = f"Step {step['n']}: {describe_step(step)}"
        else:
            caption = "The screen when the user finished."
        size = f" ({frame.get('width')}x{frame.get('height')})" if frame.get("width") else ""
        parts.append({"type": "text", "text": f"[{caption}{size}]"})
        parts.append({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")}})
        shown += 1
    if not shown:
        return None
    return {"role": "user", "content": parts}


def pending_demonstration(history: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The demonstration of the latest message when that message is the user's and carries one."""
    if not history:
        return None
    last = history[-1]
    if last.get("sender") != "user":
        return None
    demonstration = (last.get("raw_payload") or {}).get("demonstration")
    return demonstration if isinstance(demonstration, dict) and demonstration.get("id") else None

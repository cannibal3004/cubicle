"""Tool calls the model wrote as text instead of making them.

Qwen sometimes writes a call while it is still thinking: a well-formed
`<tool_call>` block inside the reasoning. The model server only turns calls
in the answer into real ones, so the block stays text, nothing runs, and the
model, with no result coming back, goes on as if one had: the likeliest next
tokens after an unanswered call are an invented answer to it. On the work
server the bot wrote two such calls, then described a container it had not
yet looked at.

This module reads those blocks. `CallWatch` follows the reasoning stream
and says when to stop it: right after the last of one or more consecutive
complete blocks, before any other text. `parse_blocks` turns blocks into
calls in the shape the model server's own tool calls have, so the turn runs
them like any other (approval, trust, results). Both of Qwen's formats are
read: the XML one,

    <tool_call>
    <function=computer_terminal_execute>
    <parameter=command>
    ls -la
    </parameter>
    </function>
    </tool_call>

and the JSON one, `<tool_call>{"name": ..., "arguments": {...}}</tool_call>`.
Parameter values are converted to the types the tool's schema asks for.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

OPEN = "<tool_call>"
CLOSE = "</tool_call>"
# A block that opens and has not closed by then is not a call being written.
MAX_BLOCK_CHARS = 60_000

_FUNCTION = re.compile(r"<function=([\w.\-:]+)>(.*?)</function>", re.S)
_PARAMETER = re.compile(r"<parameter=([\w.\-:]+)>(.*?)</parameter>", re.S)
_BLOCK = re.compile(re.escape(OPEN) + r"(.*?)" + re.escape(CLOSE), re.S)


@dataclass
class Block:
    start: int
    end: int
    body: str


def looks_like_call(body: str) -> bool:
    """A block written as a call (a <function=...> or a JSON object), not prose that names the tags."""
    return bool(_FUNCTION.search(body)) or body.strip().startswith("{")


def find_blocks(text: str) -> List[Block]:
    """Every complete <tool_call>…</tool_call> block written as a call, in order."""
    return [Block(m.start(), m.end(), m.group(1)) for m in _BLOCK.finditer(text or "") if looks_like_call(m.group(1))]


def _could_open(tail: str) -> bool:
    """`tail` (stripped of leading space) is the start of another block, or could still become one."""
    tail = tail.lstrip()
    return OPEN.startswith(tail) or tail.startswith(OPEN)


class CallWatch:
    """Follows one round's reasoning and says when it holds calls to cut at.

    `feed` returns True once the stream should stop: at least one complete
    block has been written and what follows the last one is neither space
    nor the start of another block. `finish` says whether the stream ended
    holding blocks (the model stopped right after them).
    """

    def __init__(self) -> None:
        self.text = ""
        self.cut = False
        self.closed = False  # a closing tag has been seen: from then on, look at the blocks

    def feed(self, delta: str) -> bool:
        if self.cut or not delta:
            return self.cut
        before = len(self.text)
        self.text += delta
        if not self.closed:
            # Only the new text (plus a tag's width of overlap) can complete the first block.
            if CLOSE not in self.text[max(0, before - len(CLOSE)):]:
                return False
            self.closed = True
        first = self.text.find(OPEN)
        if first < 0:
            return False
        blocks = find_blocks(self.text[first:])
        if not blocks:
            return False
        end = first + blocks[-1].end
        tail = self.text[end:]
        if not tail.strip() or _could_open(tail):
            # Space, or another block on its way: let it finish (within reason).
            if tail.lstrip().startswith(OPEN) and len(tail) > MAX_BLOCK_CHARS:
                self.cut = True
                self.cut_at = end
            return self.cut
        self.cut = True
        self.cut_at = end
        return True

    cut_at: int = -1  # where the kept text ends: right after the last complete call

    def kept_of(self, delta: str) -> str:
        """The part of the latest delta that comes before the cut (none of what follows the calls)."""
        if not self.cut or self.cut_at < 0:
            return delta
        delta_start = len(self.text) - len(delta)
        return delta[: max(0, self.cut_at - delta_start)]

    def holds_calls(self) -> bool:
        return bool(find_blocks(self.text))

    def before_calls(self) -> str:
        """The reasoning up to the first block: what is kept."""
        blocks = find_blocks(self.text)
        return self.text[: blocks[0].start] if blocks else self.text


def _schema_types(specs: Iterable[Any], name: str) -> Dict[str, str]:
    for spec in specs or []:
        if getattr(spec, "name", None) == name:
            props = ((getattr(spec, "parameters", None) or {}).get("properties") or {})
            return {key: str((value or {}).get("type") or "string") for key, value in props.items()}
    return {}


def _typed(value: str, kind: str) -> Any:
    text = value
    if kind in ("integer", "number"):
        try:
            number = float(text.strip())
            return int(number) if kind == "integer" and number.is_integer() else number
        except ValueError:
            return text
    if kind == "boolean":
        lowered = text.strip().lower()
        if lowered in ("true", "1", "yes"):
            return True
        if lowered in ("false", "0", "no"):
            return False
        return text
    if kind in ("array", "object"):
        try:
            return json.loads(text)
        except ValueError:
            return text
    return text


def _strip_value(raw: str) -> str:
    """Qwen puts each value on lines of its own: drop the one newline on each side."""
    value = raw[1:] if raw.startswith("\n") else raw
    return value[:-1] if value.endswith("\n") else value


def parse_block(body: str, specs: Iterable[Any] = ()) -> Optional[Tuple[str, Dict[str, Any]]]:
    """(tool name, arguments) from one block's body, or None when it cannot be read."""
    function = _FUNCTION.search(body)
    if function:
        name = function.group(1)
        types = _schema_types(specs, name)
        arguments = {
            key: _typed(_strip_value(raw), types.get(key, "string"))
            for key, raw in _PARAMETER.findall(function.group(2))
        }
        return name, arguments
    stripped = body.strip()
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
        except ValueError:
            return None
        if not isinstance(data, dict) or not isinstance(data.get("name"), str):
            return None
        arguments = data.get("arguments", data.get("parameters", {}))
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                return None
        return (data["name"], arguments) if isinstance(arguments, dict) else None
    return None


def parse_blocks(text: str, specs: Iterable[Any] = ()) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Calls (in the model server's shape: id, name, JSON arguments) and the bodies that could not be read."""
    calls: List[Dict[str, Any]] = []
    unreadable: List[str] = []
    for block in find_blocks(text):
        parsed = parse_block(block.body, specs)
        if parsed is None:
            unreadable.append(block.body.strip()[:200])
            continue
        name, arguments = parsed
        calls.append({"id": f"call_t{uuid.uuid4().hex[:12]}", "name": name, "arguments": json.dumps(arguments, ensure_ascii=False)})
    return calls, unreadable


def without_blocks(text: str) -> str:
    """The text with every complete block taken out (a call written in the answer)."""
    return _BLOCK.sub("", text or "").strip()

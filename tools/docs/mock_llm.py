"""A scripted stand-in for the model server, for the docs screenshots.

An OpenAI-compatible /v1/models and /v1/chat/completions (streamed) that answer a few
set prompts the same way every time, so live moments (an approval card, thinking as it
streams) can be photographed without a real model. Standard library only; the docs
stack runs it as its own service.
"""

import json
import re
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = "qwen3.8-flash-next"


def chunk(delta=None, finish=None, usage=None):
    body = {"id": "chatcmpl-docs", "object": "chat.completion.chunk", "model": MODEL,
            "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}] if delta is not None or finish else []}
    if usage:
        body["usage"] = usage
    return body


def words(text, field):
    """`text` a few words at a time, as a model streams it."""
    parts = text.split(" ")
    for index in range(0, len(parts), 3):
        yield {field: " ".join(parts[index:index + 3]) + (" " if index + 3 < len(parts) else "")}


def help_answer(raw):
    """An answer built from a cubicle_help result: the first section, its link, and a screenshot it offers."""
    try:
        result = json.loads(raw)
    except ValueError:
        return "I couldn't read the help just now."
    first = (result.get("sections") or [{}])[0]
    this_bot = result.get("this_bot") or {}
    allowed = ", ".join(f"`{c}`" for c in this_bot.get("always_allowed_commands") or []) or "none yet"
    picture = next((m.group(0) for s in result.get("sections") or [] for m in [re.search(r"!\[[^\]]*\]\([^)]+\)", s.get("text", ""))] if m), "")
    return (
        "Add it to my **always-allowed commands**: open my settings (the sliders in the chat header), go to **Tools**, and add "
        "`df -h` under *Always-allowed commands*. Next time I run it, it goes straight through. You can also tick "
        f"**Always allow for this bot** on the approval card the next time I ask.\n\nRight now my always-allowed commands are {allowed}. "
        f"More in [{first.get('title', 'the help')}]({first.get('link', '/help')}).\n\n{picture}"
    )


def script(messages):
    """What to stream for this conversation: (deltas, pause between them in seconds)."""
    last = messages[-1] if messages else {}
    user = next((m for m in reversed(messages) if m.get("role") == "user"), {})
    asked = str(user.get("content") or "").lower()
    if last.get("role") == "tool":
        raw = str(last.get("content") or "")
        if '"sections"' in raw:
            return list(words(help_answer(raw), "content")), 0.01
        text = "Okay, I've left it where it is." if "denied" in raw.lower() else "Done: it's gone from the shared folder."
        return list(words(text, "content")), 0.02
    if asked.startswith("how do i"):
        thinking = "A question about Cubicle itself: look it up in its help rather than answer from memory."
        call = {"index": 0, "id": f"call_{uuid.uuid4().hex[:12]}", "type": "function",
                "function": {"name": "cubicle_help", "arguments": json.dumps({"question": user.get("content")})}}
        return list(words(thinking, "reasoning_content")) + [{"tool_calls": [call]}], 0.02
    if "delete the old" in asked:
        thinking = "Alex wants the 2025 comparison removed. It's in reports/ in the shared folder; deleting needs Alex's approval, so ask."
        call = {"index": 0, "id": f"call_{uuid.uuid4().hex[:12]}", "type": "function",
                "function": {"name": "shared_folder_delete", "arguments": json.dumps({"path": "reports/nas-drives-2025.md"})}}
        return list(words(thinking, "reasoning_content")) + [{"tool_calls": [call]}], 0.02
    if "think it over" in asked:
        thinking = ("Let me work through this step by step. The pool is 21.8 TB with 14.2 TB used, so 7.6 TB is free. "
                    "Over the last three months usage grew by about 0.45 TB a month, but September was larger because of the photo import. "
                    "Without that one-off, the trend is closer to 0.35 TB a month. ") * 12
        return list(words(thinking, "reasoning_content")) + list(words("At the current rate there's room for well over a year.", "content")), 0.12
    if "title" in str(messages[0].get("content") or "").lower() and len(messages) <= 3:
        return list(words("Old drive comparison", "content")), 0.0
    return list(words("Sure, I can help with that.", "reasoning_content")) + list(words("Sure. What would you like me to do?", "content")), 0.02


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _json(self, body, status=200):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            self._json({"object": "list", "data": [{"id": MODEL, "object": "model", "owned_by": "docs", "context_length": 262144,
                                                   "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["text"]}}]})
        elif self.path.rstrip("/").endswith("/health"):
            self._json({"status": "ok"})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self._json({"error": {"message": "not served"}}, 404)
            return
        deltas, pause = script(body.get("messages") or [])
        usage = {"prompt_tokens": 9200, "completion_tokens": 240, "total_tokens": 9440}
        if not body.get("stream"):
            content = "".join(d.get("content", "") for d in deltas)
            self._json({"id": "chatcmpl-docs", "model": MODEL, "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}], "usage": usage})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            for delta in deltas:
                self.wfile.write(f"data: {json.dumps(chunk(delta))}\n\n".encode())
                self.wfile.flush()
                time.sleep(pause)
            finish = "tool_calls" if any("tool_calls" in d for d in deltas) else "stop"
            self.wfile.write(f"data: {json.dumps(chunk({}, finish))}\n\n".encode())
            self.wfile.write(f"data: {json.dumps(chunk(None, None, usage))}\n\ndata: [DONE]\n\n".encode())
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass  # the turn was stopped


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 9000), Handler).serve_forever()

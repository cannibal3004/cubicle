# How it works

```
 Browser ── HTTPS ──▶ Caddy ─┬─▶ web  (Next.js client)
                             └─▶ api  (FastAPI) ──┬─▶ SQLite + encryption key       (/data)
                                                  ├─▶ model server                   (OpenAI-compatible)
                                                  ├─▶ decision server                (auto-approval, Auto thinking)
                                                  ├─▶ Composio                       (connected apps)
                                                  ├─▶ MCP servers                    (stdio inside the API, or URLs)
                                                  └─▶ socket proxy ─▶ Docker ─▶ a computer per bot
```

Caddy serves the client and the API on one address, so the browser talks to one origin; it disables
buffering for the API so replies stream. The API holds all the state; the client holds none that matters.

## A reply, from start to finish

1. The client stores your message (`POST /chat/send`) and starts a turn (`POST /chat/turns/{thread}`).
   The turn runs as a task on the server, independent of any browser: the client follows its events
   over Server-Sent Events, and a reload, a second tab or another machine attaches to the same turn and
   replays what it missed.
2. The turn builds the prompt: the bot's instructions, a description of its tools, then the conversation
   as it was sent before, word for word, tool calls and results included. Nothing that changes from one
   message to the next goes near the top, so a model server with a prompt cache resumes from the end
   of the last reply: a reply costs only what it adds. What Cubicle looks up for a message (matching
   attachment passages, the bot's procedures, project log entries) rides with that message.
3. The model streams back thinking, text and tool calls. Each tool call becomes an action request that
   the action gateway checks against its registry (deny by default), pauses for approval when the action
   needs it (the approval card, or the decision model), runs, records in the audit trail, and returns to
   the model as a tool result. The model is called again, for as long as it keeps calling tools and
   making progress.
4. The reply is stored with its thinking, where each round's thinking and each tool call came in the
   text, the tool results (trimmed), and its token usage.

When a conversation outgrows `CONTEXT_COMPACT_AT` of the window, it is compacted before a turn or
between its rounds: the model summarizes the older part, the summary is stored as a marker, and the
context starts there. Notes, work notes and the to-do list ride along in the summary.

## The tools a bot is offered

The gateway decides which actions need approval; the model is told which, and to stop and ask when one
is denied.

| Tools | What they do | Ask first |
| --- | --- | --- |
| `memory_*`, `work_notes_*`, `todo_*`, `project_*`, `projects_list` | Notes, work notes, to-do list, projects and their logs | No |
| `ask_user` | A question card; waits for your answer | No |
| `share_file` | Hands you a file as a download card | No |
| `read_file`, `list_files`, `computer_screenshot`, `computer_start`, `computer_request_takeover` | Look at the bot's computer | No |
| `write_file`, `edit_file`, `computer_terminal_execute`, `computer_browser_navigate`, `computer_send_input`, `computer_install_packages` | Change things on the computer | Yes, unless trusted or always allowed |
| `shared_folder_*` | The shared folder, for a bot without a computer | Writes and deletes |
| `schedule_*` | The bot's schedules | Creating and deleting |
| `bots_*` | Orchestrators: see, make and brief other bots | Making and briefing |
| `connector_*` | Start or check an app connection | Connecting |
| `mcp_*` | Add, test, list or remove MCP servers | Adding and removing |
| `<app>_<action>` | Connected apps' tools | Unless the app marks them read-only |
| `<server>_<tool>` | MCP servers' tools | Unless marked read-only, or the server is trusted read-only |

## Where the code is

| Path | What it holds |
| --- | --- |
| `client/components/` | The UI: Dashboard (the frame), ChatWindow, MessageItem, Sidebar, the settings and project pages, the computer panel |
| `client/lib/api.js` | Every API call the client makes |
| `server/app/routers/` | The HTTP routes; `chat.py` runs a turn |
| `server/app/services/` | Everything else: storage, the action gateway, approvals and the decision gate, tools, projects, thinking, retrieval, schedules, sharing, the computer providers |
| `server/tests/` | The test suite |
| `runtime/` | The computer image: the desktop, the browser driver, the recorder |
| `tools/docs/` | The screenshot tooling for these docs |
| `docs/` | These pages |

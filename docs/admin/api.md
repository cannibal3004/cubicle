# API

Everything the web client does goes through the API, so scripts can do it too. Routes are under
`/api/v1`. Authenticate with an API token (Settings → Account → API tokens) sent as
`Authorization: Bearer <token>`; a token acts as the account that made it. The running server also
describes every route, with request and response shapes, at `/docs` (Swagger) and `/redoc` on the API's
own port.

```bash
curl -H "Authorization: Bearer $TOKEN" https://cubicle.example/api/v1/bots
```

## Talking to a bot

A reply runs on the server: you store a message, start a turn, and follow its events.

```bash
# 1. store the message
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"thread_id": "<conversation>", "bot_id": "<bot>", "user_text": "Did last night'\''s backup run?"}' \
  $BASE/api/v1/chat/send
# 2. start the reply
curl -X POST -H "Authorization: Bearer $TOKEN" $BASE/api/v1/chat/turns/<conversation>
# 3. follow it (Server-Sent Events), or poll GET /chat/turns/<conversation> until it is done
curl -N -H "Authorization: Bearer $TOKEN" $BASE/api/v1/chat/stream/<conversation>
# 4. read the stored reply
curl -H "Authorization: Bearer $TOKEN" $BASE/api/v1/chat/history/<conversation>
```

A bot's first conversation has the bot's own id; others come from `GET /bots/{id}/conversations`.

| Method | Route | What it does |
| --- | --- | --- |
| POST | `/chat/send` | Store a user message (`thread_id`, `bot_id`, `user_text`, optional attachments) |
| POST | `/chat/turns/{thread}` | Start a reply |
| GET | `/chat/turns`, `/chat/turns/{thread}` | Replies running or waiting on you |
| POST | `/chat/turns/{thread}/cancel` | Stop a reply |
| GET | `/chat/stream/{thread}` | A reply's events (SSE); numbered, so a client can resume with `after` |
| GET, DELETE | `/chat/history/{thread}` | Read a conversation's messages, or empty it |
| GET, POST | `/chat/rewind/{thread}`, `/chat/redo/{thread}` | Undo and redo |
| POST | `/chat/compact/{thread}` | Compact a conversation now |
| GET | `/chat/thinking/levels`, `/chat/thinking/picks` | The thinking levels on offer; Auto's recent picks and how the replies went |
| POST | `/approvals/respond` | Answer an approval card (`request_id`, `action`: `allow` or `deny`) |
| POST | `/questions/answer` | Answer a bot's question (`request_id`, `answer`) |
| POST | `/attachments`, `/upload` | Upload a file or an image for the next message |
| GET | `/commands` | The slash commands |
| GET | `/search?q=` | Search bots and conversations |

## Bots, conversations and projects

| Method | Route | What it does |
| --- | --- | --- |
| GET, POST | `/bots` | List your bots (and those shared with you), or make one |
| GET, PUT, DELETE | `/bots/{bot}` | Read, change or delete a bot |
| POST | `/bots/{bot}/suggestions/draft` | Draft suggestion cards from the bot's instructions |
| GET, POST | `/bots/{bot}/conversations` | A bot's conversations, or a new one (`title`, `project_id`) |
| GET, PATCH, DELETE | `/conversations/{id}` | One conversation: title, project, archive, thinking, work notes, to-do list |
| GET, POST | `/bots/{bot}/projects` | A bot's projects, or a new one |
| GET, PATCH, DELETE | `/projects/{id}` | One project: name, work notes, archive |
| GET | `/projects/{id}/log` | Its log, newest first |
| POST | `/projects/{id}/folder` | Make sure its folder exists; returns the path |
| GET, POST, PUT, DELETE | `/bots/{bot}/schedules[/{id}]`, `.../run` | Schedules, and running one now |
| GET, PUT, DELETE | `/bots/{bot}/secrets[/{name}]` | A bot's secrets (names and dates only; values are write-only) |
| GET, POST, PATCH, DELETE | `/bots/{bot}/members[/{user}]` | Who a bot is shared with |
| GET, PUT | `/bots/{bot}/sharing` | What teammates' conversations may use |

## The computer and files

| Method | Route | What it does |
| --- | --- | --- |
| GET | `/computers/{bot}` | A bot's computer and its state |
| POST | `/computers/{bot}/create`, `start`, `pause`, `stop`, `reset` | Lifecycle |
| POST | `/computers/{bot}/control` | Take or hand back control |
| POST | `/computers/{bot}/actions` | Run an action (`browser_navigate`, `terminal_execute`, ...); one that needs approval returns `pending_approval` and its request |
| POST | `/computers/{bot}/actions/{request}/execute` | Run an action once it is approved |
| POST, GET | `/computers/{bot}/record/start`, `stop`, `record` | Show the bot: recording |
| WebSocket | `/computers/{bot}/vnc` | The live desktop |
| GET | `/files/list`, `/files/download` | Browse and download (`source`: `computer` or `shared`, `path`, `bot_id`) |
| POST, DELETE | `/files/upload`, `/files/folder`, `/files` | Upload, make a folder, delete |

## Apps, MCP and the audit

| Method | Route | What it does |
| --- | --- | --- |
| GET | `/connectors/catalog`, `/connectors` | Connected-app catalog and connection status |
| POST, DELETE | `/connectors/{app}/authorize`, `/connectors/{app}` | Connect (returns the sign-in URL) or disconnect |
| GET | `/tools/catalog` | Built-in tool groups and every connected app's tools |
| GET, POST, PUT, DELETE | `/mcp/servers[/{id}]`, `.../test`, `.../reconnect`, `/mcp/test` | Your MCP servers (and system ones, for admins) |
| GET, POST, PUT, DELETE | `/mcp/bots/{bot}/servers[/{id}]`, `.../test` | A bot's own MCP servers |
| GET | `/audit` | Recent approvals, tool runs and connector events |
| GET, POST | `/gate/decisions`, `/gate/check` | Auto-approval verdicts; test the decision server |

## Accounts and settings

| Method | Route | What it does |
| --- | --- | --- |
| GET | `/health` | Server status (no sign-in needed) |
| POST | `/auth/setup` | First run only: make the owner's account |
| POST | `/auth/login`, `/auth/logout` | Sign in (username, password, two-factor code) and out |
| GET | `/auth/session`, `/auth/status`, `/auth/me` | Who is signed in; their sessions and tokens |
| POST | `/auth/profile`, `/auth/password`, `/auth/totp/start`, `confirm`, `disable`, `/auth/sessions/sign-out-others` | Your account |
| GET, POST, DELETE | `/auth/tokens[/{id}]` | API tokens (shown once) |
| GET, POST, PATCH | `/admin/users[/{id}]` | Admins: accounts |
| GET | `/people` | Accounts you can share a bot with |
| GET, POST | `/settings` | Settings; app-wide ones are admins' to change |
| GET, PUT, DELETE | `/secrets[/{name}]` | Your app-wide secrets |
| GET | `/models`, `/models/slots` | The model list; requests waiting for the model server |

# Configuration

The API reads these environment variables. With Docker Compose, put them in `.env`; `docker-compose.yml`
passes them through. Many have a counterpart in [App Settings](settings.md), which wins over the
variable once it is saved there.

## Address and sign-in

| Variable | Default | What it does |
| --- | --- | --- |
| `SITE_ADDRESS` | `https://localhost` | The address people open (compose): Caddy serves it and issues its certificate. Several, comma-separated, serve the site under each (a LAN IP and a tunnel's hostname); put the LAN IP first |
| `HTTP_PORT`, `HTTPS_PORT` | `80`, `443` | The ports Caddy listens on (compose) |
| `AUTH_COOKIE_SECURE` | `1` in compose, else `0` | `1` when served over HTTPS |
| `AUTH_SESSION_MAX_AGE` | 30 days | Lifetime of a browser session, in seconds |
| `CORS_ORIGINS` | the site address | Browser origins allowed to call the API (only matters when the API runs on another origin) |
| `HOST`, `PORT` | `127.0.0.1`, `8000` | Where the API listens when run directly |

## The model server

| Variable | Default | What it does |
| --- | --- | --- |
| `LLM_PROVIDER` | `openai_compatible` | `openai_compatible`, or the legacy `muapi` |
| `LLM_BASE_URL` | OpenRouter | The server's base URL; a local one ends in `/v1` |
| `LLM_API_KEY` | empty | Its key, when no key is saved in App Settings. Optional for local servers |
| `DEFAULT_MODEL` | empty | The model new bots start with |
| `LLM_REASONING_EFFORT` | empty | The default thinking level, sent as `reasoning_effort`; empty leaves it to the server |
| `LLM_MAX_COMPLETION_TOKENS` | `0` | Sent as `max_tokens` when set; `0` leaves the server's default |
| `LLM_MAX_CONCURRENT_REQUESTS` | `4` | Requests at a time at the model server, shared round-robin between accounts; `0` is no limit |
| `LLM_TOOLS_ENABLED` | `1` | `0` offers the model no tools, leaving only the slash commands |
| `LLM_SCREENSHOTS_TO_MODEL` | `auto` | Send computer screenshots to the model as images: `auto` (unless the model has no vision), `always`, `never` |

## Thinking

| Variable | Default | What it does |
| --- | --- | --- |
| `THINKING_LEVELS` | `none,low,medium,high,xhigh` | The levels the menus offer: the ones your model understands |
| `THINKING_AUTO_LIGHT`, `THINKING_AUTO_DEEP` | `low`, `xhigh` | The two levels [Auto](../thinking.md#auto) picks between |
| `THINKING_STYLE` | `field` | How the level reaches the model: `field` sends it as `reasoning_effort`; `hint` always sends `THINKING_HINT_BASE` and adds the level's instruction to the end of the message, so changing level keeps the server's prompt cache. Use `hint` with Qwen3.8 Flash Next on halogen-flash-server |
| `THINKING_HINT_BASE` | `medium` | The level sent in the hint style |

## Conversations and context

| Variable | Default | What it does |
| --- | --- | --- |
| `CONTEXT_COMPACT_AT` | `0.7` | Share of the context window at which a conversation is compacted; `0` turns it off (`/compact` still works) |
| `CONTEXT_KEEP_RECENT_TOKENS` | `30000` | Recent conversation kept word for word when compacting |
| `CONTEXT_STOP_AT` | `0.9` | When compaction cannot make room, tools are withdrawn at this share and the bot answers |
| `CONTEXT_TOOL_RESULT_CHARS` | `4000` | How much of each tool result is kept with the reply |
| `CONTEXT_TOOL_RESULTS_MESSAGE_CHARS` | `24000` | Tool result text kept per reply; the oldest go first |
| `LLM_MAX_TOOL_ROUNDS` | `100` | Safety net on model calls in one reply |
| `LLM_STALLED_ROUNDS` | `3` | Rounds that only repeat earlier calls with the same results before the bot is made to answer |
| `BOT_MEMORY_MAX_CHARS` | `16000` | Size of a bot's notes |
| `BOT_WORK_NOTES_MAX_CHARS` | `8000` | Size of a conversation's or project's work notes |
| `BOT_TODO_MAX_ITEMS` | `50` | Items a to-do list may hold |

## Tools, apps and approvals

| Variable | Default | What it does |
| --- | --- | --- |
| `APPROVAL_TIMEOUT_SECONDS` | `900` | How long an approval card waits for an answer |
| `QUESTION_TIMEOUT_SECONDS` | `900` | How long a bot's question to you waits |
| `AUTO_APPROVAL` | `off` | Default [auto-approval](../approvals.md#auto-approval) mode: `off`, `shadow` or `on` |
| `DECIDER_URL` | empty | The decision server (no `/v1`), for auto-approval and Auto thinking |
| `DECIDER_THRESHOLD` | `0.2` | An action is auto-approved only when every risk score is below this |
| `DECIDER_TIMEOUT_SECONDS` | `15` | How long to wait for the decision server before asking you |
| `COMPOSIO_API_KEY` | empty | The connector key, when none is saved in App Settings |
| `COMPOSIO_IMPORTANT_TOOLS_ONLY` | `1` | Offer only the tools Composio marks important for each app |
| `COMPOSIO_TOOLS_PER_TOOLKIT` | `40` | Most tools offered per connected app |
| `TOOL_SELECTION_MAX` | `40` | Above this many tools, connected apps' tools are narrowed to the ones that match the conversation; `0` offers everything |
| `TOOL_ADD_STYLE` | `list` | Where a tool that becomes available partway through a conversation goes: `list` adds it to the tool list (the server rereads the conversation); `message` describes it at the end of the message, keeping the prompt cache. Use `message` with halogen-flash-server |

## Retrieval

| Variable | Default | What it does |
| --- | --- | --- |
| `RETRIEVAL_ENABLED` | `1` | Index long attachments, project logs and notes with the model server's embedding model, and bring matching passages into a message |
| `EMBEDDING_MODEL`, `RERANK_MODEL` | empty | The models to use; empty uses the server's defaults |
| `RETRIEVAL_TOP_K` | `6` | Passages looked at per message |
| `RETRIEVAL_CHUNK_CHARS` | `1200` | Size of an indexed passage |
| `RETRIEVAL_MIN_RERANK_SCORE`, `RETRIEVAL_MIN_SCORE` | `0.3`, `0.3` | How relevant a passage must be to be brought in |
| `NPU_RETRY_ATTEMPTS`, `NPU_RETRY_BACKOFF_SECONDS`, `NPU_REQUEST_TIMEOUT_SECONDS` | `3`, `1,3`, `20` | Retries for the decision, embedding and reranking calls, which on halogen run on an NPU that can restart |

## Files

| Variable | Default | What it does |
| --- | --- | --- |
| `WORKSPACE_DIR` | `./workspace` | The shared folder (compose); with an absolute path it is mounted at `/shared` in every computer |
| `WORKSPACE_ROOT` | the repository | The shared folder when the API runs directly |
| `WORKSPACE_MAX_FILE_BYTES` | `131072` | Read and write limit of the `shared_folder_*` tools |
| `ATTACHMENT_MAX_BYTES` | 50 MB | Largest file you can attach |
| `ATTACHMENT_INLINE_MAX_CHARS` | `40000` | How much of an attachment's text goes in the message |
| `ATTACHMENT_EXTRACT_TIMEOUT` | `60` | Seconds a PDF or Office conversion may take |
| `FILE_SHARE_MAX_BYTES` | 200 MB | Largest file a bot can hand you |

## Schedules

| Variable | Default | What it does |
| --- | --- | --- |
| `TZ`, `SCHEDULE_TIMEZONE` | `UTC` | The timezone schedules are written in (`America/Chicago`); `SCHEDULE_TIMEZONE` overrides `TZ` for schedules |
| `SCHEDULE_TICK_SECONDS` | `30` | How often due schedules are checked |
| `SCHEDULES_MAX_PER_BOT` | `20` | Schedules a bot may have |
| `SCHEDULE_MIN_MINUTES` | `5` | Shortest "every" interval |
| `SCHEDULER_ENABLED` | `1` | `0` turns the scheduler off |

## The computer runtime

| Variable | Default | What it does |
| --- | --- | --- |
| `COMPUTER_PROVIDER` | `fake` | `docker` turns bots' computers on (with `COMPOSE_PROFILES=computer`) |
| `COMPUTER_DOCKER_IMAGE` | `cubicle-computer:2.14.0` | The runtime image |
| `COMPUTER_DOCKER_WORKSPACE_MODE` | `volume` in compose | `bind` keeps workspaces in a host directory, `volume` in a Docker volume per computer |
| `COMPUTER_DOCKER_HOST_WORKSPACE_ROOT` | empty | In bind mode from compose: the host path of the API's `/data/computers` |
| `COMPUTER_DOCKER_HOST_SHARED_DIR` | `WORKSPACE_DIR` when absolute | Host path of the shared folder mounted at `/shared` |
| `COMPUTER_DOCKER_SCREEN` | `1920x1200` | Screen size, set when a computer is created |
| `COMPUTER_SCREENSHOT_MAX_WIDTH` | `1280` | Screenshots to the model are scaled to this width; `0` sends them whole |
| `COMPUTER_DOCKER_CPU_LIMIT`, `COMPUTER_DOCKER_MEMORY_LIMIT` | `2.0`, `2g` | Per computer |
| `COMPUTER_DOCKER_PIDS_LIMIT` | `2048` | Process slots per computer (the browser alone uses a few hundred) |
| `COMPUTER_DOCKER_TMP_SIZE` | `2g` | Size of `/tmp`, wiped at every start |
| `COMPUTER_DOCKER_COMMAND_TIMEOUT` | `60` | Seconds a command may run when the bot sets no timeout |
| `COMPUTER_DOCKER_COMMAND_TIMEOUT_MAX` | `600` | The longest timeout the bot may ask for |
| `COMPUTER_DOCKER_NAVIGATE_TIMEOUT` | `30` | Seconds a page load may take |
| `COMPUTER_DOCKER_INSTALL_TIMEOUT` | `600` | Seconds a package install may take |
| `COMPUTER_DOCKER_START_TIMEOUT` | `20` | Seconds a computer may take to come up |
| `COMPUTER_WALLPAPER` | empty | A JPEG or PNG on the host for the desktop (compose); default is a gradient |

## Data

| Variable | Default | What it does |
| --- | --- | --- |
| `API_DATA_DIR`, `CADDY_DATA_DIR` | Docker volumes | Host paths for the API's and Caddy's data (compose) |
| `DATA_DIR` | a per-user app directory | Where the API keeps its database and key when run directly |
| `APP_ENCRYPTION_KEY` | generated in `DATA_DIR` | A Fernet key for stored credentials, when it must come from your deployment's secrets |

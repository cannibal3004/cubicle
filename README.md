# Cubicle

A local-first AI workspace for creating bot personas, chatting with models from any OpenAI-compatible server (OpenRouter, Ollama, LM Studio, llama.cpp, vLLM, or the OpenAI API), and keeping conversations on your machine. The interface is built with Next.js and React; the API is built with FastAPI and Python.

Cubicle started as a fork of [open-grok-bot](https://github.com/Anil-matcha/open-grok-bot) by Anil Chandra Naidu Matcha and keeps its local-first shape: each bot gets its own cubicle, a sandboxed computer it works in under your approval.

This is an independent open-source project and is not affiliated with xAI.

[Quick start](#quick-start) · [Configuration](#configuration) · [API](#api) · [Architecture](#architecture) · [Limitations](#limitations)

> **Status:** Prototype / active development. The project is designed for local experimentation and is not yet a production, multi-user agent platform.

## Related Projects

- [OpenRouter](https://openrouter.ai) — Default hosted provider; one key for hundreds of models behind an OpenAI-compatible API.
- [Ollama](https://ollama.com), [LM Studio](https://lmstudio.ai), [llama.cpp server](https://github.com/ggml-org/llama.cpp/tree/master/tools/server) — Local servers that expose the same API and work without a key.
- [MuAPI](https://muapi.ai) — The original provider, still supported as the legacy `muapi` option ([API reference](https://muapi.ai/docs/api-reference), [access keys](https://muapi.ai/access-keys)).
- [awesome-meta-muse-agent](https://github.com/Anil-matcha/awesome-meta-muse-agent) — copy-paste Muse agent briefs for practical workflows, connectors, and approval boundaries.
- [awesome-grok-bot](https://github.com/Anil-matcha/awesome-grok-bot) — curated bot templates for productivity, sales, marketing, operations, and personal workflows.
- [awesome-gpt-6-astra](https://github.com/Anil-matcha/awesome-gpt-6-astra) — evidence-backed model workflows, prompts, evaluations, and safety notes.

## What it does

- **Bot personas:** Create and edit bots in a dialog with their own avatar, accent colour, role, description, model, and system prompt. Archive a bot to hide it while keeping its history, or delete it to remove the conversation and its computer too.
- **Tool control:** A Tools menu in the chat header switches tool groups and connected apps on or off for that bot, the bot editor picks individual tools, and Plugins can withhold an app's tools from every bot without disconnecting it.
- **Model picker:** Search the live model list reported by your server, or type any model ID. Models that advertise reasoning or vision support are tagged. The default model is `x-ai/grok-4.5` on OpenRouter.
- **Server-side turns:** A reply runs as a server task, not inside the browser connection. Close the tab, reload, or open the same bot on another machine and the chat reattaches to the turn in progress and replays it. Approvals keep waiting until you answer, and the sidebar marks bots that are working or need you.
- **Image attachments:** Upload JPEG, PNG, WEBP, GIF, or AVIF images. They are sent inline to vision-capable models as data URLs, so the image never leaves your machine except as part of the model request.
- **Markdown messages:** Render assistant replies as Markdown in the chat transcript.
- **Visible thinking:** Reasoning models' thinking (`reasoning_content` or `reasoning` in the stream) is shown live in a collapsible block above the reply. It is stored for display but never replayed to the model.
- **Voice dictation:** Use the browser's Web Speech API when the browser supports it.
- **Model tool calling:** The model receives OpenAI-style tool definitions for the shared folder, its sandboxed computer (when the Docker runtime is on), and connected GitHub, and can call them itself. Every call goes through the same deny-by-default gateway and approval cards as the slash commands; screenshots are shown to vision models as images.
- **Append-only context:** The transcript is replayed to the model exactly as it was sent before, tool results included, so a server with a prompt cache (halogen, llama.cpp, vLLM) resumes from the end of the last turn and a turn costs only what it adds. Nothing per-turn goes at the top of the prompt: the date sits at the end of the system prompt at day resolution. When the thread passes `CONTEXT_COMPACT_AT` of the window, before a turn or between its tool rounds, it is compacted: the model summarizes everything but the newest `CONTEXT_KEEP_RECENT_TOKENS` of conversation, the summary is stored as a marker in the thread, and the context starts there. A turn in progress carries on with its own tool calls intact. `/compact` does it on demand.
- **Slash commands:** Type `/` in the composer for a picker. `/help`, `/clear`, `/compact`, `/model`, `/auto`, `/stop`, `/computer`, `/audit`, and `/settings` are handled in the app; `/shared` and `/connector` become approval-gated actions. The list comes from `GET /api/v1/commands`.
- **Shared folder:** A directory on the host that you and every bot see. A bot's computer has it mounted at `/shared`, so the bot works there with its ordinary file tools and shell; a bot without a computer uses the approval-gated `shared_folder_*` tools, and `/shared list|read|write|delete` does the same from the composer. Everything stays inside `WORKSPACE_ROOT` and produces audit events.
- **Governed action gateway:** Shared folder actions use a structured request/result contract, a deny-by-default registry, approval state, and redacted lifecycle audit records.
- **Settings drawer:** Choose the provider, base URL, API key, default model, optional `reasoning_effort`, Composio key, and local profile details.
- **Connector surface:** Browse a curated or live Composio app catalog, inspect connection status, and start OAuth authorization explicitly from Marketplace.
- **Read-only GitHub action:** After connecting GitHub, run an explicit issue lookup from chat and pass its structured result through the action gateway.
- **Approval-gated GitHub write:** Propose a GitHub issue from chat, inspect the repository/title/body-size preview, approve it, and receive a normalized issue result.
- **Audit trail:** Review local approval, shared-folder, and connector events from the sidebar.
- **Sandbox desktop:** Each bot can have a sandboxed Ubuntu desktop (Plank dock with Chromium, a file manager and a terminal) that the model drives through its tools. You watch it live in the Computer tab over VNC, can take control with your own mouse and keyboard, and hand it back. The bot can ask you to take over for logins, CAPTCHAs, or checks.

## Quick start

### Requirements

- Node.js and npm
- Python and pip
- Either an OpenRouter API key, or a local OpenAI-compatible server such as Ollama, LM Studio, or llama.cpp

### 1. Clone the repository

```bash
git clone https://github.com/Anil-matcha/open-grok-bot.git
cd cubicle
```

### 2. Start the FastAPI server

In a terminal window:

```bash
cd server
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt

# OpenRouter (default base URL):
export LLM_API_KEY="your_openrouter_api_key"

# ...or a local server, no key needed. For example Ollama:
# export LLM_BASE_URL="http://localhost:11434/v1"
# export DEFAULT_MODEL="llama3.2"

python run.py
```

The API starts at `http://127.0.0.1:8000`.

Everything above can also be set later from **App Settings → Model server** in the UI, which is the easier route when switching between a hosted provider and a local model. The provider presets there fill in the usual base URLs:

| Server | Base URL |
| --- | --- |
| OpenRouter | `https://openrouter.ai/api/v1` |
| Ollama | `http://localhost:11434/v1` |
| LM Studio | `http://localhost:1234/v1` |
| llama.cpp server | `http://localhost:8080/v1` |
| OpenAI | `https://api.openai.com/v1` |

Any server that implements `POST /chat/completions` with `stream: true` works. If it also implements `GET /models`, the picker lists its models; otherwise type the model ID into the picker's search box.

API documentation is available at:

- Swagger UI: `http://127.0.0.1:8000/docs`
- ReDoc: `http://127.0.0.1:8000/redoc`

### 3. Start the Next.js client

In a second terminal window:

```bash
cd client
npm install
npm run dev
```

Open [http://localhost:3000](http://localhost:3000).

You can also enter the provider key from **App Settings → Connections** after the UI loads. The environment variable is the server-side fallback.

The local server creates a mode-0600 session token in `DATA_DIR` and the browser establishes an HttpOnly session automatically when connecting from loopback. For a deployment accessed from other machines, set `APP_AUTH_TOKEN` on the server; the client then shows a sign-in screen that exchanges the token for an HttpOnly session cookie. The token is never embedded in the client bundle. See [Run the whole stack with Docker Compose](#run-the-whole-stack-with-docker-compose).

### Optional Docker computer runtime

The default provider is the deterministic local adapter. To enable the real sandbox desktop (browser, terminal, file manager, input, screenshots, and live VNC), build the pinned runtime image and opt in to the Docker provider:

```bash
docker build -t cubicle-computer:2.3.0 ./runtime
export COMPUTER_PROVIDER=docker
export COMPUTER_DOCKER_IMAGE=cubicle-computer:2.3.0
```

The Docker daemon must be running before starting the API. Each bot gets a separate container, a separate workspace under `DATA_DIR/computers`, an ephemeral loopback-only port, and an internal runtime token. The sandbox user has no capabilities and cannot gain any; CPU, memory, process, and shared-memory limits are applied.

**What persists.** Beside each computer's workspace directory the API keeps `<id>.home` (the sandbox user's home: venvs, npm and cargo installs, dotfiles, the Chromium profile), `<id>.apt` (apt's download cache) and `<id>.packages` (the Ubuntu packages the computer has installed, one per line, editable). The container's own filesystem is disposable: at every start it begins as root, installs the package list, hands the home directory to the sandbox user and drops privileges before the desktop comes up, so the image can be rebuilt at any time and every computer comes back with its packages. The bot installs system packages with `computer_install_packages` (approval required; apt runs as root through `docker exec`, the sandbox shell has no sudo), and everything user-level from the shell. `/tmp` is a tmpfs of `COMPUTER_DOCKER_TMP_SIZE` (2g) wiped at every start. "Reset" in the Computer tab empties the home directory and the package list; the workspace stays.

**Secrets.** API keys and tokens a bot needs go in App Settings → Secrets (for every bot) or in the bot editor → Secrets (for one bot, overriding the app-wide value of the same name). They are stored encrypted with the same key as the provider credentials, the API never returns a value once set (`GET /api/v1/secrets` and `GET /api/v1/bots/{id}/secrets` list names and dates), and they reach a bot only as environment variables inside its computer, read at the computer's next start. The model is told the names, not the values. Since a shell can still print them, every tool result is scrubbed of the known values (`[secret NAME]`) before the model, the transcript or the screen sees it, and so are the encodings a shell or a library prints on its own: base64 (`[secret NAME as base64]`), hex and percent-encoding in a URL. That scrubbing is a guard against accidents, not a security boundary: it does not read screenshots, it does not catch a value cut in half by the output cap or transformed in some other way, and a bot that has a key in its computer can send that key anywhere its shell can reach, which is what the key is there for. A secret in a bot's computer is as safe as that bot's instructions and the model running it.

Inside the container an Xvnc display runs a minimal desktop (xfwm4 with compositing): a centred, auto-hiding Plank dock with Chromium, Thunar and xfce4-terminal, no application menu and no session controls. Chromium is not running at boot: the dock button, the bot, and the sandbox shell all start it with the same launcher command, and the driver attaches to that one instance over the loopback DevTools port, so whoever opens a window, there is a single browser. Windows have maximize and close buttons only; there is no task list, so minimizing is disabled to keep windows from being stranded. Screenshots capture the whole desktop and input goes through xdotool, so the bot can use the terminal and file manager as well as the browser. Ubuntu 24.04 with Node, Python with pip and venv, git, curl, jq and unzip is available; `/workspace` and the home directory persist between starts and `/tmp` is wiped.

**The shared folder inside the computer.** When the API knows the host path of the shared folder (`COMPUTER_DOCKER_HOST_SHARED_DIR`, which compose fills from an absolute `WORKSPACE_DIR`; an API running directly on the host uses `WORKSPACE_ROOT`), every computer gets it mounted at `/shared`. The bot is told that `/workspace` is its own project space and `/shared` is the folder you can see, and it is not offered the host-side `shared_folder_*` tools at all, so there is one filesystem to reason about. `share_file` accepts `/shared/...` paths from the computer. Because the API and the sandbox run as different unprivileged users, both sides create files in the folder writable by everyone (the API sets the modes explicitly; the sandbox runs with `umask 000`), the same arrangement as the world-writable per-computer directories. The folder itself must be writable by both users as well: a plain `chmod 777` on it is the simplest way.

**Watching and taking control.** The Computer tab streams the desktop through noVNC. The WebSocket goes to the API, which checks your session and bridges to the sandbox driver using the per-computer token; the sandbox publishes no ports and its VNC server listens on loopback only. "Take control" lets your mouse and keyboard through; while you hold control the bot's click, type and navigate tools are refused (it can still take screenshots). "Hand back to bot" returns control. The bot has a `computer_request_takeover` tool: it posts a card in chat and the Computer tab with what it needs, and after you hand control back the chat input is pre-filled with a message telling it to continue.

This is a local development runtime, not a hardened hostile-web sandbox. Follow the [official Playwright Docker guidance](https://playwright.dev/docs/docker) and do not send untrusted websites or credentials through it until network egress, image provenance, and stronger sandboxing are reviewed for your deployment.

### Run the whole stack with Docker Compose

The compose file runs the API, the client, and a Caddy reverse proxy so the whole app is reachable from other machines on one origin:

```bash
cp .env.example .env
# edit .env: set APP_AUTH_TOKEN (openssl rand -hex 32) and SITE_ADDRESS
docker compose up --build -d
```

Open the `SITE_ADDRESS` you configured and sign in with the token. Caddy routes `/api/*` to the FastAPI container and everything else to the Next.js container, with SSE buffering disabled so chat streams normally.

Caddy serves the site only for requests whose hostname or IP matches `SITE_ADDRESS` (the port may differ). Opening the server by another name, for example `http://localhost` on the host itself when `SITE_ADDRESS` is the LAN IP, returns a short message saying which address to use rather than the app.

**HTTPS on a LAN.** With an `https://` address Caddy issues a certificate from its own internal certificate authority for that hostname or IP, so browsers will warn until they trust the root certificate. An IP address works too: browsers send no SNI for bare IPs, and the compose file derives Caddy's `default_sni` from `SITE_ADDRESS` so the handshake still succeeds. Export the root certificate once and install it on each client machine:

```bash
docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt ./cubicle-ca.crt
```

HTTPS is what makes voice dictation work from another machine, since browsers only grant microphone access on secure origins. If you would rather skip certificates, set `SITE_ADDRESS=http://<hostname-or-ip>` and `AUTH_COOKIE_SECURE=0`.

**Local model servers.** Inside the API container, `localhost` is the container. A server on the Docker host is reachable as `http://host.docker.internal:<port>/v1`; a server on another machine by its LAN address. Enter it in App Settings or as `LLM_BASE_URL` in `.env`. The same applies to the auto-approval decision server (`DECIDER_URL`, no `/v1` suffix).

**Shared folder.** `WORKSPACE_DIR` (default `./workspace`) is the shared folder: the directory the approved `shared_folder_*` tools and `/shared` commands can read, write and delete in, and the one mounted at `/shared` inside every bot's computer when the path is absolute (compose cannot hand a relative path to the Docker daemon; set `COMPUTER_DOCKER_HOST_SHARED_DIR` to the absolute path instead). Point it at a project directory on the Docker host to let bots work on real files there.

**Upgrading from Open Grok Bot.** The project was renamed; the API adopts a pre-rename `~/.open-grok-bot` data directory and renames an `open-grok-bot.sqlite3` database in place on first start, sessions are re-established on next login (the cookie name changed), and runtime containers and the compose project are recreated under the new name. Rebuild the runtime image (`docker compose --profile computer build computer`) and remove old sandbox containers with `docker rm -f $(docker ps -aq --filter label=open-grok-bot.runtime=computer)`.

**Data and backups.** The API keeps everything it persists under `/data`: the SQLite database (bots, history, settings with encrypted keys, approvals, audit) and, in volume workspace mode, one folder per sandbox computer. Caddy keeps its internal certificate authority and issued certificates under its own `/data`. By default both are Docker named volumes (`api_data`, `caddy_data`). To keep them in a directory your backups already cover, set `API_DATA_DIR` and `CADDY_DATA_DIR` in `.env` to host paths; the API runs as uid 10001, so make its directory writable by that user (`chown -R 10001:10001 <dir>`). To move an existing installation, stop the stack, copy the volume contents (`docker run --rm -v open-grok-bot_api_data:/from -v <dir>:/to alpine cp -a /from/. /to/`), set the variables, and start it again. `caddy_config` only holds Caddy's last applied config and can stay a volume.

**Computer runtime (optional).** The compose stack can also run the Docker/Playwright computer runtime. It is off by default because it requires the API to control the host's Docker daemon. To enable it, set these two lines in `.env` and bring the stack up again:

```bash
COMPUTER_PROVIDER=docker
COMPOSE_PROFILES=computer
```

This builds the runtime image and starts a `docker-socket-proxy` sidecar on an internal-only network. The API never sees the Docker socket; it talks to the proxy, which only permits the container operations the provider uses (create, start, pause, kill, inspect images). Runtime containers join a dedicated `cubicle-computers` network and are reached by name, so no ports are published for them. Each computer gets a named Docker volume as its workspace. To keep sandbox workspaces on the host instead, set `COMPUTER_DOCKER_WORKSPACE_MODE=bind` and `COMPUTER_DOCKER_HOST_WORKSPACE_ROOT` to the host path of the API's `/data/computers` directory (with `API_DATA_DIR=/srv/cubicle/data` that is `/srv/cubicle/data/computers`). The API creates one folder per computer there and makes it world-writable, because the sandbox runs as a different unprivileged user than the API.

Runtime containers are started by the API, not by compose, so `docker compose down` does not remove any that are still running. Stop computers from the UI first, or remove them by label:

```bash
docker rm -f $(docker ps -q --filter label=cubicle.runtime=computer)
```

The security caveats from the [Optional Docker computer runtime](#optional-docker-computer-runtime) section apply unchanged.

## Configuration

The client defaults to `http://127.0.0.1:8000/api/v1`. Set `NEXT_PUBLIC_API_URL` if the API runs elsewhere. In the compose build it is set to the relative path `/api/v1` because Caddy serves both on one origin.

```bash
NEXT_PUBLIC_API_URL="http://127.0.0.1:8000/api/v1"
```

The server reads these variables from the environment:

| Variable | Default | Purpose |
| --- | --- | --- |
| `LLM_PROVIDER` | `openai_compatible` | `openai_compatible` or the legacy `muapi` |
| `LLM_API_KEY` | empty | Provider credential used when no key is saved in local settings. Optional for local servers |
| `LLM_BASE_URL` | per provider | API base URL. Defaults to `https://openrouter.ai/api/v1` for `openai_compatible` and `https://api.muapi.ai/api/v1` for `muapi` |
| `LLM_REASONING_EFFORT` | empty | When set it is sent verbatim as `reasoning_effort`. Common values are `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`; which ones work depends on the server and model. Leave empty to let the server use its default |
| `LLM_TOOLS_ENABLED` | `1` | Offer the governed tools to the model through function calling. Set `0` to keep only the explicit slash commands |
| `LLM_MAX_TOOL_ROUNDS` | `100` | Safety net on model round-trips in one turn. A turn normally ends when the model answers, when the context is full (`CONTEXT_STOP_AT`, default 0.9 of the window, after compaction could not make room) or when it stops making progress (`LLM_STALLED_ROUNDS`, default 3 consecutive rounds that only repeat earlier calls with the same results). In every case the model gets one final call without tools so it still answers |
| `BOT_MEMORY_MAX_CHARS` | `16000` | Size of a bot's notes in characters. They are rendered in full after a compaction and after a full rewrite, so this bounds that one-time context cost |
| `BOT_TODO_MAX_ITEMS` | `50` | Items a bot's to-do list may hold. The whole list comes with every todo tool result and every compaction |
| `LLM_SCREENSHOTS_TO_MODEL` | `auto` | Send sandbox screenshots to the model as images: `auto` (unless the model catalog says it has no vision), `always`, or `never` |
| `CONTEXT_COMPACT_AT` | `0.7` | Share of the model's context window at which the thread is compacted: everything but the most recent turns is summarized into one stored message and the context starts there. Checked before a turn and between its tool rounds. `0` turns the automatic trigger off; `/compact` always works |
| `CONTEXT_KEEP_RECENT_TOKENS` | `30000` | How much recent conversation, in tokens, is kept verbatim when compacting. Whole turns are kept, newest first, until the budget is spent; the newest turn is always kept |
| `CONTEXT_TOOL_RESULT_CHARS` | `4000` | How much of each tool result is kept with the reply |
| `CONTEXT_TOOL_RESULTS_MESSAGE_CHARS` | `24000` | Total tool result text kept per reply (oldest results are dropped first) |
| `MUAPI_API_KEY`, `MUAPI_BASE_URL` | empty | Legacy names. When `LLM_PROVIDER` is unset and `MUAPI_API_KEY` is present, the provider defaults to `muapi` and these values are used |
| `COMPOSIO_API_KEY` | empty | Optional connector credential used when no key is saved in local settings |
| `COMPOSIO_IMPORTANT_TOOLS_ONLY` | `1` | Offer the model only the tools Composio marks important for each connected app |
| `COMPOSIO_TOOLS_PER_TOOLKIT` | `40` | Maximum tools offered to the model per connected app |
| `DEFAULT_MODEL` | `x-ai/grok-4.5` (`grok-4-5` for MUAPI) | Initial model used for new settings and bots |
| `DATA_DIR` | per-user hidden app directory | SQLite database, migration copies, and local key location |
| `APP_ENCRYPTION_KEY` | generated mode-0600 key in `DATA_DIR` | Optional Fernet key for encrypted provider credentials |
| `APP_AUTH_TOKEN` | generated mode-0600 token in `DATA_DIR` | Access token for the sign-in screen and bearer auth; disables automatic loopback sessions when set |
| `AUTH_SESSION_MAX_AGE` | `86400` | Session-cookie lifetime in seconds |
| `AUTH_COOKIE_SECURE` | `0` | Set to `1` when serving over HTTPS |
| `CORS_ORIGINS` | localhost and loopback client origins | Comma-separated browser origins allowed by the API |
| `COMPUTER_PROVIDER` | `fake` | Computer adapter: `fake` or `docker` |
| `COMPUTER_DOCKER_IMAGE` | `cubicle-computer:2.3.0` | Pinned local runtime image |
| `COMPUTER_DOCKER_WORKSPACE_ROOT` | `DATA_DIR/computers` | Root for per-bot runtime workspaces |
| `COMPUTER_DOCKER_CPU_LIMIT` | `2.0` | Docker CPU limit per computer |
| `COMPUTER_DOCKER_MEMORY_LIMIT` | `2g` | Docker memory limit per computer |
| `COMPUTER_DOCKER_PIDS_LIMIT` | `512` | Maximum processes per computer |
| `COMPUTER_DOCKER_START_TIMEOUT` | `20` | Runtime readiness timeout in seconds |
| `COMPUTER_DOCKER_COMMAND_TIMEOUT` | `30` | Docker/driver operation timeout in seconds |
| `COMPUTER_DOCKER_NETWORK` | empty | When set, runtime containers join this Docker network and are reached by name instead of a port published on loopback. Used by the compose stack |
| `COMPUTER_DOCKER_WORKSPACE_MODE` | `bind` | `bind` mounts a host directory per computer; `volume` uses a named Docker volume per computer |
| `COMPUTER_DOCKER_HOST_WORKSPACE_ROOT` | empty | In `bind` mode from inside a container: the host path equivalent of `COMPUTER_DOCKER_WORKSPACE_ROOT` |
| `COMPUTER_DOCKER_WALLPAPER` | empty | Absolute host path of a JPEG or PNG used as the sandbox desktop wallpaper (compose: `COMPUTER_WALLPAPER`). Default is a generated gradient |
| `COMPUTER_DOCKER_HOST_SHARED_DIR` | `WORKSPACE_DIR` when absolute, else `WORKSPACE_ROOT` for an API that sees host paths | Host path of the shared folder, mounted at `/shared` in every computer. Empty: no mount, and bots with a computer keep the host-side tools |
| `WORKSPACE_ROOT` | repository root | The shared folder: the directory the `shared_folder_*` tools and `/shared` commands are confined to |
| `WORKSPACE_MAX_FILE_BYTES` | `131072` | Read/write size limit for the host-side shared folder tools |
| `APPROVAL_TIMEOUT_SECONDS` | `900` | How long a pending approval remains open. Turns run server-side, so this can be generous |
| `AUTO_APPROVAL` | `off` | Default auto-approval mode: `off`, `shadow` (score and log, the user still decides), or `on`. App Settings overrides it; each bot can override that |
| `DECIDER_URL` | empty | Base URL of the decision server used for auto-approval (a halogen-flash-server with `decider-4b`, or any server with `POST /v1/systemone`) |
| `DECIDER_THRESHOLD` | `0.2` | An action is auto-approved only when every risk score is below this |
| `DECIDER_TIMEOUT_SECONDS` | `15` | How long to wait for the decision server before falling back to asking the user |
| `HOST` | `127.0.0.1` | FastAPI bind address |
| `PORT` | `8000` | FastAPI port |

For `openai_compatible`, chat requests go to `{LLM_BASE_URL}/chat/completions` and the model list comes from `{LLM_BASE_URL}/models`. For `muapi`, the selected model ID is appended to the base URL and long requests are polled at `/predictions/{id}/result`.

A key saved before the provider setting existed is migrated automatically: the database keeps using MUAPI with that key until you change the provider in App Settings.

## App surfaces

| Surface | Purpose |
| --- | --- |
| Chat | Bot roster, model selection, Markdown replies, image attachments, voice dictation, and streamed responses |
| Computer | Bot-scoped provider lifecycle, screen metadata polling, and governed computer actions |
| Marketplace | Searchable curated or live app catalog with explicit connect/disconnect actions |
| Audit trail | Recent approval, shared-folder, and connector events persisted by the local API |
| App Settings | Local profile values, model server connection, default model, and reasoning effort |

## Architecture

```
Next.js client  ── HTTP + SSE ──▶  FastAPI server
                                     │
                 ┌───────────────────┼───────────────────┐
                 ▼                   ▼                   ▼
           SQLite + key store   LLM provider        Optional Composio
           state/settings/audit  OpenAI-compatible   connector endpoints
                                 or legacy MUAPI
                                     │
                                     ▼
                         Computer provider
                       fake or Docker/Playwright
                              + action gateway
```

The main code areas are:

| Path | Responsibility |
| --- | --- |
| `client/app/` | Next.js app entry points and global styles |
| `client/components/` | Dashboard, chat, model picker, settings, marketplace, audit, and preview surfaces |
| `client/lib/api.js` | HTTP and EventSource client functions |
| `server/app/main.py` | FastAPI app, CORS, router registration, and health route |
| `server/app/routers/` | Bots, chat, models, uploads, settings, approvals, connectors, and computers |
| `server/app/services/llm_service.py` | Provider dispatch for chat streaming and the model catalog |
| `server/app/services/llm_config.py` | Resolves provider, base URL, key, reasoning effort, and default model from env and saved settings |
| `server/app/services/openai_compatible_service.py` | Streaming `/chat/completions` client and `/models` normalisation for OpenAI-compatible servers |
| `server/app/services/muapi_service.py` | Legacy MUAPI requests, prediction polling, and the static MUAPI model registry |
| `server/app/services/storage_service.py` | SQLite persistence, legacy import, secrets, and default data |
| `server/app/services/database.py` | SQLite connection management and schema migrations |
| `server/app/services/secret_store.py` | Fernet encryption for provider credentials |
| `server/app/services/workspace_service.py` | Confined list/read/write/delete tools for the shared folder |
| `server/app/services/approval_broker.py` | Pending approval coordination and audit events |
| `server/app/services/approval_gate.py` | Auto-approval: scores pending actions with a decision model and logs every verdict |
| `server/app/services/action_gateway.py` | Registered action policy, approval handoff, execution, and lifecycle audit |
| `server/app/services/composio_service.py` | Composio v3 REST calls (auth configs, connected accounts, tool execution) and normalized GitHub issue results |
| `server/app/services/connector_actions.py` | Explicit connector command parsing and gateway registration |
| `server/app/services/computer_provider.py` | Provider contract and deterministic local computer adapter |
| `server/app/services/docker_computer_provider.py` | Per-bot Docker lifecycle, token boundary, and runtime operations |
| `server/app/services/computer_actions.py` | Computer action definitions and gateway executors |
| `server/app/routers/computers.py` | Authenticated computer lifecycle, screen, and approval continuation routes |
| `runtime/` | Pinned Playwright container image and authenticated runtime driver |
| `server/app/schemas/contracts.py` | Pydantic request and response models |
| `server/tests/` | Focused shared folder, approval, persistence, auth, and provider regression tests |

### Chat request flow

1. The client posts the user message to `/api/v1/chat/send`.
2. The server stores it in the local message store.
3. The client starts a turn with `POST /api/v1/chat/turns/{thread_id}`. The server runs it as its own task, independent of any browser connection, and the client follows it over Server-Sent Events from `/api/v1/chat/stream/{thread_id}`. Events are numbered and buffered, so a reload, a second tab, or another machine attaches to the running turn and replays what it missed; `GET /api/v1/chat/turns` lists turns that are running or waiting on an approval, which drives the sidebar indicators.
4. An explicit shared folder or connector request becomes a structured action request and is checked against the deny-by-default gateway registry.
5. The gateway pauses the stream for approval when required, then executes the registered action.
6. The gateway emits normalized action lifecycle records; the client receives compatible tool events and the structured result.
7. The server sends the system prompt (including a description of the available tools), full conversation history, any slash-command result, and the tool definitions to the configured provider as an OpenAI-style `messages` array (MUAPI receives a flattened prompt and no tools).
8. If the model calls tools, each call is validated, turned into a gateway action, approved where required, executed, and its result is appended as a `tool` message; the model is then called again, for as long as it has room and keeps making progress (see `LLM_MAX_TOOL_ROUNDS`).
9. Provider output is forwarded as SSE deltas and the completed assistant message is persisted together with its thinking and the tools it used.

### Product direction

The prototype follows a local-first path: persistent bot personas and histories, explicit capabilities, user-visible approvals, provider flexibility, optional app connectors, and an opt-in isolated browser runtime. The next meaningful layers are durable memory and routines, voice input/output, human takeover, desktop/VNC presentation, background jobs, and authenticated multi-user deployment. They are intentionally documented as roadmap items rather than implied by the current UI.

### Tools the model can call

With an OpenAI-compatible provider the model is offered these functions. The gateway registry decides which need approval; the model is told which ones do and is instructed to stop and ask when a call is denied.

| Tool | Gateway action | Approval |
| --- | --- | --- |
| `shared_folder_list`, `shared_folder_read`, `shared_folder_write`, `shared_folder_delete` | `workspace.*` | yes (not offered to a bot whose computer has the folder at `/shared`) |
| `share_file` | `files.share` | no (puts a download card for a shared folder or computer file in the chat) |
| `memory_read`, `memory_write`, `memory_edit` | `memory.*` | no (the bot's own notes, see below) |
| `todo_list`, `todo_add`, `todo_update` | `todo.*` | no (the bot's working list, see below) |
| `computer_start`, `computer_screenshot`, `computer_files_list`, `computer_read_file`, `computer_request_takeover` | `computer.*` | no |
| `computer_browser_navigate`, `computer_terminal_execute`, `computer_write_file`, `computer_edit_file`, `computer_send_input`, `computer_install_packages` | `computer.*` | yes |
| `<app>_<action>` for every app connected through Composio, e.g. `gmail_fetch_emails`, `github_create_a_pull_request` | `connector.composio_read` or `connector.composio_action` | no for tools Composio marks read-only, yes for everything else |
| `github_list_issues`, `github_create_issue` | `connector.github_*` | no / yes (fallback when the Composio catalog cannot be fetched) |

### Bot notes (memory)

Each bot keeps one small document across conversations and compactions: the goal, decisions, where things are, how to run things. The bot reads it with `memory_read`, starts or reorganises it with `memory_write`, and changes one passage with `memory_edit` (an exact, unique find-and-replace; the result shows only the changed passage, so a one-line change does not cost a full rewrite). The notes are shown in the bot editor, where the user can edit them too; such edits appear in the chat so the model sees them. They are never put in the system prompt, which keeps the provider's prompt cache intact: the bot's own writes stay in the transcript and each compaction summary carries the current notes. `BOT_MEMORY_MAX_CHARS` (default 16000) bounds the size.

### Bot to-do list

Notes are for what stays true; the to-do list is for what is left to do on the job at hand. The bot adds the steps with `todo_add` and moves each through open, doing and done with `todo_update` (by a short id such as `t3`, so a change is one small call), and every result carries the whole list. The list is shown above the chat, where you can tick, reword, add and remove items; your edits appear in the thread so the model sees them. Like the notes it never enters the system prompt: the bot's own changes stay in the transcript and each compaction summary carries the current list, so work survives a compaction or a turn that was cut off. When a turn's tools are withdrawn (context full, no progress, round cap) and items remain unfinished, the bot is asked to name them in its reply so the next turn picks them up. `BOT_TODO_MAX_ITEMS` (default 50) bounds the size.

### Auto-approval

Every tool that needs approval still opens an approval card. With auto-approval on, a small decision model scores the proposed action while the card is up and, when every risk score is low, answers the card on your behalf; the chat shows which calls it approved (a lightning mark on the tool chip) and the card says why. Anything it is unsure about, and anything it cannot score, waits for you exactly as before.

The model is asked five plain yes/no questions about the action and the message that led to it: does it permanently delete or discard data, upload local data somewhere, read secrets or change system settings, download and run code, or do something irreversible such as sending a message or publishing. Two things are decided by rule instead: a write that shrinks an existing file to a quarter of its size or less is always held, since the model cannot tell a rewrite from a wipe, and pointer or keyboard input to a bot computer is not scored (it auto-approves in **On**). It sees the tool name, its display arguments (a path and byte count, never file contents), and the user's latest message; it never sees tool output, so text the bot read cannot talk the gate into anything. It runs as `decider-4b` (or `decider-0.8b`) on a halogen-flash-server NPU, scoring an action in about a second.

Set it up in **App Settings → Auto-approval**: the server URL (with a Test button), the mode, and the risk threshold. **Shadow** is the mode to start with: approvals behave exactly as before, and **Audit → Auto-approval** shows what the model would have approved next to what you actually decided, including the actions you denied that it would have let through. Switch to **On** once that list looks right. A bot's Tools menu can override the mode for that bot alone. Every scored action is kept in the local database (`gate_decisions`) with its scores, the exact state the model saw, and the final decision, so the log doubles as labelled data for tuning the questions or the threshold later.

Computer tools appear only when `COMPUTER_PROVIDER=docker`. Connector tools appear for each toolkit with an active Composio connection. If the toolkit's auth config in the Composio dashboard restricts the tools available for execution, exactly those tools are offered; otherwise only the tools Composio flags as important are, at most `COMPOSIO_TOOLS_PER_TOOLKIT` per app (see `COMPOSIO_IMPORTANT_TOOLS_ONLY`). Results are trimmed to 20 KB before they are returned to the model, and a screenshot is attached as an image message when the model accepts images.

### Approved shared folder commands

These commands are intentionally explicit; arbitrary shell commands are not accepted:

```text
/shared list [path]
/shared read <path>
/shared write <path>
file content on the next line
/shared delete <path>
```

Paths must remain inside `WORKSPACE_ROOT`. Reads and writes are limited by `WORKSPACE_MAX_FILE_BYTES`, delete removes a file or a whole folder, and every request/decision/result is recorded in the local audit store.

Connector actions are intentionally explicit:

```text
/connector github issues <owner>/<repo> [open|closed|all]
/connector github create-issue <owner>/<repo> <title>
optional body on the next line
```

The read action requires a configured Composio key and an active GitHub connection. The write action also requires an approval response. Results are limited to issue summaries; credentials and issue bodies are not copied into the action request preview or audit summary.

Computer actions are also explicit. Lifecycle controls use the authenticated `/computers/{bot_id}/...` routes and are recorded by the action gateway. Provider operations such as `terminal_execute`, `browser_navigate`, `files_list`, and `send_input` can be opened through `/computers/{bot_id}/actions`; higher-risk operations return a pending request until `/approvals/respond` allows them, then the matching `/actions/{request_id}/execute` route continues the action.

## API

All routes are prefixed with `/api/v1`.

| Method | Endpoint | Description |
| --- | --- | --- |
| GET | `/health` | Check server status and default model |
| GET | `/auth/status` | Report authentication state without exposing credentials |
| GET | `/auth/session` | Establish a local or bearer-backed browser session |
| POST | `/auth/login` | Exchange a configured bearer token for an HttpOnly session |
| POST | `/auth/logout` | Clear the current browser session |
| GET, POST | `/bots` | List or create bot personas |
| PUT, DELETE | `/bots/{bot_id}` | Update a bot (including `archived`), or delete it with its messages, running turn, and computer |
| GET | `/models` | Return the configured model catalog |
| GET | `/chat/history/{thread_id}` | Read a bot's message history |
| POST | `/chat/send` | Store a user message |
| GET | `/chat/stream/{thread_id}?model=...` | Stream a response over SSE |
| POST | `/upload` | Validate and upload an image attachment |
| GET, POST | `/settings` | Read public settings or save write-only credentials and app settings |
| POST | `/approvals/respond` | Submit an Allow/Deny approval response |
| POST | `/gate/check` | Probe the auto-approval decision server with a harmless action |
| GET | `/gate/decisions?limit=200` | What the auto-approval gate was shown and concluded, with the final decision |
| GET | `/audit?limit=100` | Read recent approval, tool, and connector events |
| GET | `/tools/catalog` | Built-in tool groups and every connected app's tools, with the globally withheld apps |
| GET | `/files/download?source=&path=&bot_id=` | Download (or `inline=1` preview) a file a bot shared; confined to the shared folder or the bot's computer workspace |
| GET | `/connectors/catalog` | Return curated or Composio-backed connector cards |
| GET | `/connectors?services=...` | Check connector connection status |
| POST | `/connectors/{slug}/authorize` | Request an OAuth URL |
| DELETE | `/connectors/{slug}` | Disconnect a connector |
| GET | `/computers/{bot_id}` | Read provider status without creating a runtime |
| POST | `/computers/{bot_id}/create` | Create the bot-scoped provider runtime |
| POST | `/computers/{bot_id}/start` | Start the provider runtime |
| POST | `/computers/{bot_id}/pause` | Pause the provider runtime |
| POST | `/computers/{bot_id}/stop` | Stop the provider runtime |
| POST | `/computers/{bot_id}/reset` | Reset provider state and generation |
| GET | `/computers/{bot_id}/health` | Read provider health through the gateway |
| GET | `/computers/{bot_id}/screenshot` | Read the current screen-state metadata |
| POST | `/computers/{bot_id}/actions` | Open a provider operation, returning 202 when approval is required |
| POST | `/computers/{bot_id}/actions/{request_id}/execute` | Continue an approved provider operation |

Try the health route after starting the server:

```bash
curl http://127.0.0.1:8000/api/v1/health
```

## Local data and secrets

On first start, the server creates a SQLite database under the per-user data directory defined in `server/app/config.py`. Bots, messages, settings, approvals, and audit events survive restarts, with schema migrations and a local owner identity tracked in the database. Existing JSON files are imported once and retained as migration copies; credential fields in the old settings file are scrubbed after import.

For local development:

- API routes require authentication. Loopback browser clients receive a session automatically; direct API clients can read `.auth-token` from `DATA_DIR` and send `Authorization: Bearer <token>`.
- For non-loopback access, set `APP_AUTH_TOKEN` explicitly and sign in through the UI (or call `/auth/login`) to obtain the session cookie. Do not expose the token through logs or source control.
- Provider credentials are encrypted at rest with a mode-0600 Fernet key in `DATA_DIR`. Set `APP_ENCRYPTION_KEY` when the key must be supplied by deployment secrets or shared across restarts and hosts.
- Settings responses never return provider credentials. Enter a new value to replace a stored key, or leave it blank to keep the current one.
- Back up the SQLite database and encryption key together. If the key is lost, encrypted credentials must be entered again.
- Keep `CORS_ORIGINS` narrow and use HTTPS plus `AUTH_COOKIE_SECURE=1` outside local development.
- Never commit API keys, local settings, transcripts, or generated environment files.
- The repository ignores local SQLite files, encryption keys, `.env` files, virtual environments, caches, and build output.

## Limitations

The following surfaces are present but should not be mistaken for completed infrastructure:

- **Single local owner:** Authentication, bearer validation, and owner-scoped rows are present, but user provisioning, roles beyond the local owner, and multi-user grants are still pending.
- **Single-user local storage:** SQLite improves restart durability, but multi-user provisioning, backups, and multi-instance coordination are still pending.
- **Docker runtime is opt-in:** The Docker/Playwright provider and image are included, but live container startup depends on a running Docker daemon and a locally built image. The current runtime has browser screenshots and controlled operations, not a full desktop or VNC session.
- **No routines:** Bots keep notes and a to-do list across conversations, but there is no scheduled routine engine or background worker yet.
- **No voice or multi-client apps:** Voice, desktop, and mobile clients are not included in this repository.
- **Shared folder and computer tools are intentionally narrow:** Shared folder commands support confined file listing, reads, writes and deletes. Docker computer operations run in a bot-scoped container and require approval for browser navigation, terminal commands, and input; human takeover and unrestricted desktop control are not implemented.
- **Secret redaction is best-effort:** Tool results are scrubbed of each secret's plain, JSON-escaped, base64, hex and URL-encoded forms. Screenshots, truncated values and any other transformation pass through. Treat the computer's secrets as available to the bot, because they are.
- **Connector actions are intentionally narrow:** Chat currently exposes only GitHub issue listing and approval-gated issue creation. Dynamic tool discovery, arbitrary connector calls, and other connector writes remain roadmap work.
- **Provider streaming is adapter-level:** Depending on the provider response, the service may receive a completed result and emit it to the UI in small deltas.
- **No CI workflow is included yet:** Focused shared folder, approval, auth, persistence, and provider tests are present, but broader runtime integration and browser tests remain to be added.

## Development

Available client scripts:

```bash
cd client
npm run dev       # Start the development server
npm run build     # Create a production build
npm run start     # Serve the production build
npm run lint      # Run the configured Next.js lint command
```

Run the focused backend tests with:

```bash
DATA_DIR=/tmp/cubicle-test-data PYTHONPATH=server python -m unittest discover -s server/tests -v
```

When changing an API contract, update the Pydantic schema, router, client helper, tests, and this README together.

## Troubleshooting

### The UI says the API is offline

Confirm that the FastAPI server is running on port 8000, then check:

```bash
curl http://127.0.0.1:8000/api/v1/health
```

If the server runs on another host or port, set `NEXT_PUBLIC_API_URL` before starting the client.

If protected API calls return `401`, confirm the browser origin is listed in `CORS_ORIGINS`. For non-loopback deployments the UI shows the sign-in screen; enter the `APP_AUTH_TOKEN` value. Behind the compose proxy both the page and the API share one origin, so CORS does not apply.

### The Docker computer stays unavailable

Confirm that the daemon is running, the runtime image was built, and the API uses the Docker adapter:

```bash
docker info
docker image inspect cubicle-computer:2.3.0
echo "$COMPUTER_PROVIDER"
```

The API intentionally reports a failed Docker start instead of silently executing the task in the fake adapter. Keep `COMPUTER_PROVIDER=fake` when Docker is not available.

### The chat returns a provider error

The error text from the server is shown in the chat as the reply. Common causes:

- **HTTP 401 / 403:** the key is missing or wrong. Open App Settings → Model server and save a key.
- **HTTP 404 on `/chat/completions`:** the base URL is wrong. For Ollama, LM Studio and llama.cpp it must end in `/v1`.
- **Unknown or unloaded model:** the model ID is not one the server offers. Use the picker's refresh button to reload the list, or load the model in your local server first.
- **Unrecognized argument `reasoning_effort`:** the server or model does not accept it. Set Reasoning effort back to "Not sent" in App Settings.
- **Could not reach ...:** the server is not running or is not listening on that host and port.

### The model picker is empty or shows only the default model

The server did not answer `GET /models`. The picker still accepts a typed model ID, so search for the ID you want and choose "Use ... as the model ID".

### Images are ignored by the model

Images are sent as OpenAI-style `image_url` parts. The selected model must support image input; on OpenRouter such models are tagged "Vision" in the picker. For local servers choose a vision model such as a LLaVA or Qwen-VL variant.

## Contributing

Focused issues and pull requests are welcome. Before opening a change:

- Keep setup and behavior documentation synchronized with the code.
- Do not include secrets, personal data, or local transcripts.
- Explain changes to provider behavior, API contracts, or persistence.
- Run the client checks that apply to your change.

## License

This repository does not currently include a license file. Add an explicit license before distributing it as a reusable package.

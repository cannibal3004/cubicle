import os
from pathlib import Path, PurePosixPath, PureWindowsPath

def _resolve_llm_provider() -> str:
    """Pick the LLM provider, honouring the legacy MUAPI-only environment."""
    explicit = os.getenv("LLM_PROVIDER", "").strip().lower().replace("-", "_")
    if explicit in {"openai_compatible", "muapi"}:
        return explicit
    # Older deployments only set MUAPI_API_KEY. Keep them working unchanged.
    if os.getenv("MUAPI_API_KEY", "").strip():
        return "muapi"
    return "openai_compatible"


def _default_data_dir() -> str:
    """~/.cubicle, unless only the pre-rename ~/.open-grok-bot exists."""
    new = Path.home() / ".cubicle"
    legacy = Path.home() / ".open-grok-bot"
    if not new.exists() and legacy.exists():
        return str(legacy)
    return str(new)


def _absolute_or_empty(value: str) -> str:
    """`value` when it is an absolute path on either kind of host, else ""."""
    value = (value or "").strip()
    if PureWindowsPath(value).is_absolute() or PurePosixPath(value).is_absolute():
        return value
    return ""


class Settings:
    # Any OpenAI-compatible chat completions server (OpenRouter, Ollama,
    # LM Studio, llama.cpp, vLLM, ...) or the legacy MUAPI prediction API.
    LLM_PROVIDER: str = _resolve_llm_provider()
    LLM_API_KEY: str = (
        os.getenv("LLM_API_KEY", "").strip()
        or (os.getenv("MUAPI_API_KEY", "").strip() if LLM_PROVIDER == "muapi" else "")
    )
    LLM_BASE_URL: str = (
        os.getenv("LLM_BASE_URL", "").strip()
        or (os.getenv("MUAPI_BASE_URL", "").strip() if LLM_PROVIDER == "muapi" else "")
    ).rstrip("/")
    LLM_REASONING_EFFORT: str = os.getenv("LLM_REASONING_EFFORT", "").strip().lower()
    # Let the model call the governed tools (workspace, computer, connectors)
    # through OpenAI-style function calling. Approvals still apply.
    LLM_TOOLS_ENABLED: bool = os.getenv("LLM_TOOLS_ENABLED", "1").strip().lower() in {"1", "true", "yes"}
    # Sent as max_tokens when set. 0 leaves the server's own default, which
    # on some local servers is 8192 and cuts a long tool call in half.
    LLM_MAX_COMPLETION_TOKENS: int = int(os.getenv("LLM_MAX_COMPLETION_TOKENS", "0"))
    # How many requests may be in flight at the model server at once, shared
    # fairly between accounts; the rest queue in the chat with their place
    # shown. 0 means no limit. App Settings (llm_max_concurrent) overrides it.
    LLM_MAX_CONCURRENT_REQUESTS: int = max(0, int(os.getenv("LLM_MAX_CONCURRENT_REQUESTS", "4")))
    # A safety net, not the working limit: a turn normally ends when the
    # model answers, when the context is full (CONTEXT_STOP_AT) or when it
    # stops making progress (LLM_STALLED_ROUNDS).
    LLM_MAX_TOOL_ROUNDS: int = int(os.getenv("LLM_MAX_TOOL_ROUNDS", "100"))
    # Consecutive tool rounds whose every call repeats an earlier call of the
    # turn with the same result before the model is asked to answer. 0 disables.
    LLM_STALLED_ROUNDS: int = int(os.getenv("LLM_STALLED_ROUNDS", "3"))
    # Size of a bot's notes (memory_write / memory_edit and the bot editor).
    # The notes are rendered in full after every compaction and after every
    # full rewrite, so this bounds a one-time context cost, not a per-turn one.
    BOT_MEMORY_MAX_CHARS: int = int(os.getenv("BOT_MEMORY_MAX_CHARS", "16000"))
    # How many items a bot's to-do list may hold. The list travels with every
    # todo tool result and every compaction, so this bounds that cost.
    BOT_TODO_MAX_ITEMS: int = int(os.getenv("BOT_TODO_MAX_ITEMS", "50"))
    # Work notes: where a project (or a conversation without one) stands. Kept short on purpose.
    BOT_WORK_NOTES_MAX_CHARS: int = int(os.getenv("BOT_WORK_NOTES_MAX_CHARS", "8000"))
    # Scheduled tasks: the zone their times are written in (TZ is the usual
    # name; SCHEDULE_TIMEZONE overrides it for schedules only), how often due
    # schedules are checked, and bounds. SCHEDULER_ENABLED=0 turns the loop off.
    SCHEDULE_TIMEZONE: str = (os.getenv("SCHEDULE_TIMEZONE") or os.getenv("TZ") or "UTC").strip()
    SCHEDULE_TICK_SECONDS: float = float(os.getenv("SCHEDULE_TICK_SECONDS", "30"))
    SCHEDULER_ENABLED: bool = os.getenv("SCHEDULER_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")
    SCHEDULES_MAX_PER_BOT: int = int(os.getenv("SCHEDULES_MAX_PER_BOT", "20"))
    SCHEDULE_MIN_MINUTES: int = int(os.getenv("SCHEDULE_MIN_MINUTES", "5"))
    # Screenshots as images for the model: auto (unless the catalog says the
    # model has no vision), always, or never.
    LLM_SCREENSHOTS_TO_MODEL: str = os.getenv("LLM_SCREENSHOTS_TO_MODEL", "auto").strip().lower()
    # Context management. Tool results are stored with the reply that made
    # them (trimmed) and the most recent tool-using bot turns are replayed to the model
    # with those results so it does not redo work; older turns get stubs.
    # Compaction: when the last reported prompt reaches this share of the
    # model's context window, the next turn first summarises everything but
    # the most recent user turns. 0 disables the automatic trigger.
    CONTEXT_COMPACT_AT: float = float(os.getenv("CONTEXT_COMPACT_AT", "0.7"))
    CONTEXT_KEEP_RECENT_TOKENS: int = int(os.getenv("CONTEXT_KEEP_RECENT_TOKENS", "30000"))
    # Share of the window at which a turn stops calling tools when compaction
    # could not make room (nothing older to fold, or the summary failed).
    CONTEXT_STOP_AT: float = float(os.getenv("CONTEXT_STOP_AT", "0.9"))
    CONTEXT_TOOL_RESULT_CHARS: int = int(os.getenv("CONTEXT_TOOL_RESULT_CHARS", "4000"))
    CONTEXT_TOOL_RESULTS_MESSAGE_CHARS: int = int(os.getenv("CONTEXT_TOOL_RESULTS_MESSAGE_CHARS", "24000"))
    COMPOSIO_API_KEY: str = os.getenv("COMPOSIO_API_KEY", "")
    # Connected apps are offered to the model as tools. Composio flags a
    # subset of each toolkit as "important"; that keeps the list useful
    # without flooding the context window.
    COMPOSIO_IMPORTANT_TOOLS_ONLY: bool = os.getenv("COMPOSIO_IMPORTANT_TOOLS_ONLY", "1").strip().lower() in {"1", "true", "yes"}
    COMPOSIO_TOOLS_PER_TOOLKIT: int = int(os.getenv("COMPOSIO_TOOLS_PER_TOOLKIT", "40"))
    DEFAULT_MODEL: str = os.getenv("DEFAULT_MODEL", "").strip() or (
        "grok-4-5" if LLM_PROVIDER == "muapi" else "x-ai/grok-4.5"
    )
    DATA_DIR: Path = Path(
        os.getenv("DATA_DIR") or _default_data_dir()
    ).expanduser().resolve()
    WORKSPACE_ROOT: Path = Path(
        os.getenv("WORKSPACE_ROOT", str(Path(__file__).resolve().parents[2]))
    ).expanduser().resolve()
    WORKSPACE_MAX_FILE_BYTES: int = int(os.getenv("WORKSPACE_MAX_FILE_BYTES", "131072"))
    # Largest file a bot may hand to the user as a download card.
    FILE_SHARE_MAX_BYTES: int = int(os.getenv("FILE_SHARE_MAX_BYTES", str(200 * 1024 * 1024)))
    # Files attached to a message: the upload size limit, how much of a
    # file's text goes into the model's context, and how long extraction
    # (PDF and Office conversion) may take before the model is told to use
    # the file on disk instead.
    ATTACHMENT_MAX_BYTES: int = int(os.getenv("ATTACHMENT_MAX_BYTES", str(50 * 1024 * 1024)))
    ATTACHMENT_INLINE_MAX_CHARS: int = int(os.getenv("ATTACHMENT_INLINE_MAX_CHARS", "40000"))
    ATTACHMENT_EXTRACT_TIMEOUT: float = float(os.getenv("ATTACHMENT_EXTRACT_TIMEOUT", "60"))
    # Retrieval: embeddings (and a reranker when the server has one) on the
    # model server, used to bring the relevant parts of long attachments to a
    # turn. Empty model names let the server pick its defaults. App Settings
    # overrides all three.
    RETRIEVAL_ENABLED: str = os.getenv("RETRIEVAL_ENABLED", "1").strip()
    EMBEDDING_MODEL: str = os.getenv("EMBEDDING_MODEL", "").strip()
    RERANK_MODEL: str = os.getenv("RERANK_MODEL", "").strip()
    RETRIEVAL_TOP_K: int = int(os.getenv("RETRIEVAL_TOP_K", "6"))
    RETRIEVAL_CHUNK_CHARS: int = int(os.getenv("RETRIEVAL_CHUNK_CHARS", "1200"))
    # What a looked-up passage must score to be attached to a message: the
    # reranker's relevance (0-1), or the embedding similarity when there is no
    # reranker. Below them a passage is noise (an unrelated file scores ~0.0
    # reranked and ~0.15-0.2 by embedding; a matching one 0.9+ and 0.35+).
    RETRIEVAL_MIN_RERANK_SCORE: float = float(os.getenv("RETRIEVAL_MIN_RERANK_SCORE", "0.3"))
    RETRIEVAL_MIN_SCORE: float = float(os.getenv("RETRIEVAL_MIN_SCORE", "0.3"))
    # When a bot could be offered more tools than this, the connected apps' tools
    # are narrowed to the ones that match the message (built-ins always stay).
    # 0 offers everything. App Settings (tool_selection_max) overrides it.
    TOOL_SELECTION_MAX: int = int(os.getenv("TOOL_SELECTION_MAX", "40"))
    # Turns run server-side and outlive the page, so an approval can wait
    # for someone to come back to it.
    APPROVAL_TIMEOUT_SECONDS: int = int(os.getenv("APPROVAL_TIMEOUT_SECONDS", "900"))
    # How long a bot's question to the user (ask_user) waits for an answer.
    QUESTION_TIMEOUT_SECONDS: int = int(os.getenv("QUESTION_TIMEOUT_SECONDS", "900"))
    # Auto-approval: a decision model (decider-4b on a halogen-flash-server
    # NPU, or anything serving POST /v1/systemone) scores each action that
    # needs approval. These are the defaults; App Settings overrides them.
    # off: never consulted; shadow: scored and logged, the user still decides;
    # on: low-risk actions are approved without asking.
    AUTO_APPROVAL: str = os.getenv("AUTO_APPROVAL", "off").strip().lower()
    DECIDER_URL: str = os.getenv("DECIDER_URL", "").strip().rstrip("/")
    DECIDER_THRESHOLD: float = min(0.99, max(0.01, float(os.getenv("DECIDER_THRESHOLD", "0.2"))))
    DECIDER_TIMEOUT_SECONDS: float = float(os.getenv("DECIDER_TIMEOUT_SECONDS", "15"))
    # Thinking set to Auto picks one of these two reasoning efforts per message
    # (services/thinking.py), asking the same decision model.
    THINKING_AUTO_LIGHT: str = os.getenv("THINKING_AUTO_LIGHT", "low").strip().lower()
    THINKING_AUTO_DEEP: str = os.getenv("THINKING_AUTO_DEEP", "xhigh").strip().lower()
    # The levels the menus and /think offer: the ones the model understands
    # (model servers do not say). Qwen3.8 Flash Next on halogen has low, medium
    # and xhigh (its default), and "none" turns thinking off.
    THINKING_LEVELS: str = os.getenv("THINKING_LEVELS", "none,low,medium,high,xhigh").strip().lower()
    # How a level reaches the model: "field" sends it as reasoning_effort; "hint"
    # always sends THINKING_HINT_BASE and adds the level's instruction to the end
    # of the user's message, so changing level keeps the prompt cache (for chat
    # templates that turn the effort into a sentence near the top of the prompt,
    # like Qwen3.8 Flash Next's; see services/thinking.py).
    THINKING_STYLE: str = os.getenv("THINKING_STYLE", "field").strip().lower()
    THINKING_HINT_BASE: str = os.getenv("THINKING_HINT_BASE", "medium").strip().lower()
    # The decider, the embedding model and the reranker run on the model server's
    # NPU, which can crash and restart within seconds: each call gets this many
    # attempts, pausing these seconds between them (per-attempt timeouts are short).
    NPU_RETRY_ATTEMPTS: int = max(1, int(os.getenv("NPU_RETRY_ATTEMPTS", "3")))
    NPU_RETRY_BACKOFF_SECONDS: str = os.getenv("NPU_RETRY_BACKOFF_SECONDS", "1,3").strip()
    NPU_REQUEST_TIMEOUT_SECONDS: float = float(os.getenv("NPU_REQUEST_TIMEOUT_SECONDS", "20"))
    # Browser sessions are server-side records; this is their lifetime.
    AUTH_SESSION_MAX_AGE: int = int(os.getenv("AUTH_SESSION_MAX_AGE", str(30 * 86400)))
    AUTH_COOKIE_SECURE: bool = os.getenv("AUTH_COOKIE_SECURE", "0").lower() in {"1", "true", "yes"}
    COMPUTER_PROVIDER: str = os.getenv("COMPUTER_PROVIDER", "fake").strip().lower()
    COMPUTER_DOCKER_IMAGE: str = os.getenv(
        "COMPUTER_DOCKER_IMAGE", "cubicle-computer:2.13.0"
    ).strip()
    COMPUTER_DOCKER_BINARY: str = os.getenv("COMPUTER_DOCKER_BINARY", "docker").strip()
    COMPUTER_DOCKER_WORKSPACE_ROOT: Path = Path(
        os.getenv("COMPUTER_DOCKER_WORKSPACE_ROOT", str(DATA_DIR / "computers"))
    ).expanduser().resolve()
    COMPUTER_DOCKER_CPU_LIMIT: str = os.getenv("COMPUTER_DOCKER_CPU_LIMIT", "2.0").strip()
    COMPUTER_DOCKER_MEMORY_LIMIT: str = os.getenv("COMPUTER_DOCKER_MEMORY_LIMIT", "2g").strip()
    COMPUTER_DOCKER_PIDS_LIMIT: int = int(os.getenv("COMPUTER_DOCKER_PIDS_LIMIT", "2048"))
    # The computer's screen, WIDTHxHEIGHT; set when a computer is created, so an
    # existing one keeps its size until it is reset.
    COMPUTER_DOCKER_SCREEN: str = os.getenv("COMPUTER_DOCKER_SCREEN", "1920x1200").strip().lower()
    # Screenshots handed to the model are scaled to this width when wider; 0
    # sends the full screen. App Settings (computer_screenshot_max_width) overrides it.
    COMPUTER_SCREENSHOT_MAX_WIDTH: int = max(0, int(os.getenv("COMPUTER_SCREENSHOT_MAX_WIDTH", "1280")))
    COMPUTER_DOCKER_START_TIMEOUT: float = float(
        os.getenv("COMPUTER_DOCKER_START_TIMEOUT", "20")
    )
    # How long a terminal command may run inside the runtime when the model
    # sets no limit of its own, and the most it may ask for. The driver
    # enforces the limit (a command that hits it returns exit code 124 with
    # its output so far); the API waits that long plus a margin. A page
    # navigation has its own fixed limit.
    COMPUTER_DOCKER_COMMAND_TIMEOUT: float = float(
        os.getenv("COMPUTER_DOCKER_COMMAND_TIMEOUT", "60")
    )
    COMPUTER_DOCKER_COMMAND_TIMEOUT_MAX: float = float(
        os.getenv("COMPUTER_DOCKER_COMMAND_TIMEOUT_MAX", "600")
    )
    COMPUTER_DOCKER_NAVIGATE_TIMEOUT: float = float(
        os.getenv("COMPUTER_DOCKER_NAVIGATE_TIMEOUT", "30")
    )
    COMPUTER_DOCKER_RUNTIME_PORT: int = int(os.getenv("COMPUTER_DOCKER_RUNTIME_PORT", "3000"))
    # When the API itself runs in a container (docker compose), runtime
    # containers join this Docker network and are reached by name instead of
    # through a port published on the host's loopback.
    COMPUTER_DOCKER_NETWORK: str = os.getenv("COMPUTER_DOCKER_NETWORK", "").strip()
    # "bind" mounts a host directory per computer; "volume" uses a named Docker
    # volume per computer, which works wherever the API cannot see host paths.
    COMPUTER_DOCKER_WORKSPACE_MODE: str = os.getenv("COMPUTER_DOCKER_WORKSPACE_MODE", "bind").strip().lower()
    # In bind mode from inside a container: the host path that corresponds to
    # COMPUTER_DOCKER_WORKSPACE_ROOT, because the daemon resolves mount sources
    # on the host.
    COMPUTER_DOCKER_HOST_WORKSPACE_ROOT: str = os.getenv("COMPUTER_DOCKER_HOST_WORKSPACE_ROOT", "").strip()
    # The shared folder (WORKSPACE_ROOT) as the Docker daemon sees it, mounted
    # into every computer at /shared so a bot reaches it with its computer
    # tools. Compose passes WORKSPACE_DIR, which only works when it is an
    # absolute host path; an API that sees host paths itself (bind mode with
    # no translation) uses WORKSPACE_ROOT. Empty: computers get no /shared.
    COMPUTER_DOCKER_HOST_SHARED_DIR: str = (
        os.getenv("COMPUTER_DOCKER_HOST_SHARED_DIR", "").strip()
        or _absolute_or_empty(os.getenv("WORKSPACE_DIR", ""))
        or (str(WORKSPACE_ROOT) if COMPUTER_DOCKER_WORKSPACE_MODE == "bind" and not COMPUTER_DOCKER_HOST_WORKSPACE_ROOT else "")
    )
    # Optional host path of an image file to use as the sandbox desktop
    # wallpaper. Resolved by the Docker daemon, so it is a host path even when
    # the API runs in a container.
    COMPUTER_DOCKER_WALLPAPER: str = os.getenv("COMPUTER_DOCKER_WALLPAPER", "").strip()
    # Size of the sandbox's /tmp (a tmpfs, wiped at every start). The home
    # directory and /workspace are on disk and persist, so this only needs to
    # hold builds and scratch files.
    COMPUTER_DOCKER_TMP_SIZE: str = os.getenv("COMPUTER_DOCKER_TMP_SIZE", "2g").strip()
    # How long one apt install (computer_install_packages, or restoring the
    # package list at start) may take.
    COMPUTER_DOCKER_INSTALL_TIMEOUT: float = float(os.getenv("COMPUTER_DOCKER_INSTALL_TIMEOUT", "600"))
    COMPUTER_DOCKER_SECCOMP_PROFILE: Path = Path(
        os.getenv(
            "COMPUTER_DOCKER_SECCOMP_PROFILE",
            str(Path(__file__).resolve().parents[2] / "runtime" / "seccomp_profile.json"),
        )
    ).expanduser().resolve()
    CORS_ORIGINS = [
        origin.strip()
        for origin in os.getenv(
            "CORS_ORIGINS",
            "http://127.0.0.1:3000,http://localhost:3000",
        ).split(",")
        if origin.strip()
    ]
    HOST: str = os.getenv("HOST", "127.0.0.1")
    PORT: int = int(os.getenv("PORT", "8000"))

    def __init__(self):
        self.DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.COMPUTER_DOCKER_WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)

settings = Settings()

import os
from pathlib import Path

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
    # Turns run server-side and outlive the page, so an approval can wait
    # for someone to come back to it.
    APPROVAL_TIMEOUT_SECONDS: int = int(os.getenv("APPROVAL_TIMEOUT_SECONDS", "900"))
    # Auto-approval: a decision model (decider-4b on a halogen-flash-server
    # NPU, or anything serving POST /v1/systemone) scores each action that
    # needs approval. These are the defaults; App Settings overrides them.
    # off: never consulted; shadow: scored and logged, the user still decides;
    # on: low-risk actions are approved without asking.
    AUTO_APPROVAL: str = os.getenv("AUTO_APPROVAL", "off").strip().lower()
    DECIDER_URL: str = os.getenv("DECIDER_URL", "").strip().rstrip("/")
    DECIDER_THRESHOLD: float = min(0.99, max(0.01, float(os.getenv("DECIDER_THRESHOLD", "0.2"))))
    DECIDER_TIMEOUT_SECONDS: float = float(os.getenv("DECIDER_TIMEOUT_SECONDS", "15"))
    AUTH_SESSION_MAX_AGE: int = int(os.getenv("AUTH_SESSION_MAX_AGE", "86400"))
    AUTH_COOKIE_SECURE: bool = os.getenv("AUTH_COOKIE_SECURE", "0").lower() in {"1", "true", "yes"}
    COMPUTER_PROVIDER: str = os.getenv("COMPUTER_PROVIDER", "fake").strip().lower()
    COMPUTER_DOCKER_IMAGE: str = os.getenv(
        "COMPUTER_DOCKER_IMAGE", "cubicle-computer:2.1.1"
    ).strip()
    COMPUTER_DOCKER_BINARY: str = os.getenv("COMPUTER_DOCKER_BINARY", "docker").strip()
    COMPUTER_DOCKER_WORKSPACE_ROOT: Path = Path(
        os.getenv("COMPUTER_DOCKER_WORKSPACE_ROOT", str(DATA_DIR / "computers"))
    ).expanduser().resolve()
    COMPUTER_DOCKER_CPU_LIMIT: str = os.getenv("COMPUTER_DOCKER_CPU_LIMIT", "2.0").strip()
    COMPUTER_DOCKER_MEMORY_LIMIT: str = os.getenv("COMPUTER_DOCKER_MEMORY_LIMIT", "2g").strip()
    COMPUTER_DOCKER_PIDS_LIMIT: int = int(os.getenv("COMPUTER_DOCKER_PIDS_LIMIT", "512"))
    COMPUTER_DOCKER_START_TIMEOUT: float = float(
        os.getenv("COMPUTER_DOCKER_START_TIMEOUT", "20")
    )
    COMPUTER_DOCKER_COMMAND_TIMEOUT: float = float(
        os.getenv("COMPUTER_DOCKER_COMMAND_TIMEOUT", "30")
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

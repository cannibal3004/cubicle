from typing import List, Optional, Dict, Any, Literal
from pydantic import BaseModel, Field

from app.config import settings

class Bot(BaseModel):
    id: str
    name: str
    role: str
    description: str
    avatar: str
    model: str = Field(default_factory=lambda: settings.DEFAULT_MODEL)
    accent_color: str = "cyan"
    system_prompt: str
    tools: List[str] = []
    pinned: bool = False
    archived: bool = False
    archived_at: Optional[str] = None
    # Which tools this bot may use: {"groups": {id: bool}, "toolkits":
    # {slug: bool}, "tools": {name: bool}}; anything absent is enabled.
    tool_settings: Dict[str, Any] = Field(default_factory=dict)
    # Auto-approval for this bot: "inherit" follows App Settings; "off",
    # "shadow", or "on" override it for this bot only.
    auto_approval: Literal["inherit", "off", "shadow", "on"] = "inherit"
    # The decider's risk threshold for this bot alone; None follows App Settings.
    gate_threshold: Optional[float] = None
    # Tools (by name) this bot runs without an approval card, whatever the
    # auto-approval mode: the user's standing "yes" for this bot alone.
    trusted_tools: List[str] = Field(default_factory=list)
    # Shell commands this bot may run without asking, as word prefixes
    # ("git status", "npm test", "cat"); a script passes only when every
    # command it runs matches one. Read by services/command_rules.
    allowed_commands: List[str] = Field(default_factory=list)
    # An orchestrator sees the user's other bots, creates new ones and hands them work.
    orchestrator: bool = False
    # How-tos the bot wrote from what the user showed it (services/procedures).
    procedures: List[Dict[str, Any]] = Field(default_factory=list)
    # The bot's own notes, kept across conversations and compactions. The
    # bot writes them with memory_write; the user can edit them too.
    memory: str = ""
    memory_updated_at: Optional[str] = None
    memory_limit: int = Field(default_factory=lambda: settings.BOT_MEMORY_MAX_CHARS)
    # The bot's working list for the job at hand: [{id, text, state}].
    todo: List[Dict[str, Any]] = Field(default_factory=list)
    todo_updated_at: Optional[str] = None
    todo_limit: int = Field(default_factory=lambda: settings.BOT_TODO_MAX_ITEMS)
    unread_count: int = 0
    created_at: str

class Message(BaseModel):
    id: str
    thread_id: str
    bot_id: str
    sender: str  # "user" | "bot" | "system"
    text: str
    created_at: str
    model: Optional[str] = None
    item_type: Optional[str] = "assistant_text"  # assistant_text, tool_call, approval_card
    image_url: Optional[str] = None
    raw_payload: Optional[Dict[str, Any]] = None
    # Files the user attached: download-card fields plus where the bot finds them.
    attachments: Optional[List[Dict[str, Any]]] = None

class AttachmentRef(BaseModel):
    """An uploaded file, named the way download cards name files."""

    source: str  # 'computer' or 'workspace'
    path: str


class TurnRequest(BaseModel):
    thread_id: str
    bot_id: str
    user_text: str
    model: Optional[str] = None
    image_url: Optional[str] = None
    attachments: Optional[List[AttachmentRef]] = None
    # A demonstration the user just recorded on the bot's computer (services/demonstrations).
    recording_id: Optional[str] = None


class ApprovalDecision(BaseModel):
    request_id: str
    action: Literal["allow", "deny"]


class QuestionAnswer(BaseModel):
    """The user's answer to a question a bot asked (ask_user)."""

    request_id: str
    answer: str = ""
    option_index: Optional[int] = None


class ActionRequest(BaseModel):
    """The stable envelope used before a side-effecting action executes."""

    request_id: str
    thread_id: str
    bot_id: str
    tool: str
    action: str
    intent: str
    target: Dict[str, Any] = Field(default_factory=dict)
    arguments: Dict[str, Any] = Field(default_factory=dict)
    preview: str
    risk: Literal["read", "write", "external"] = "read"
    requires_approval: bool = True
    state: Literal["pending_approval", "approved"] = "pending_approval"
    created_at: str


class ActionResult(BaseModel):
    """The normalized result returned after an action is decided and run."""

    request_id: str
    status: Literal["completed", "failed", "denied", "expired"]
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    created_at: str


class ModelInfo(BaseModel):
    id: str
    name: str
    provider: str
    description: str = ""
    recommended: bool = False
    is_available: bool = True
    # None means the server did not say. OpenRouter advertises both; Ollama,
    # LM Studio and llama.cpp currently advertise neither.
    supports_reasoning: Optional[bool] = None
    supports_vision: Optional[bool] = None
    context_length: Optional[int] = None


class ModelCatalog(BaseModel):
    provider: str
    base_url: str
    source: Literal["remote", "static", "fallback"]
    error: Optional[str] = None
    models: List[ModelInfo] = []

class AppSettingsSchema(BaseModel):
    llm_provider: Literal["openai_compatible", "muapi"] = Field(
        default_factory=lambda: settings.LLM_PROVIDER
    )
    llm_api_key: str = Field(default="", json_schema_extra={"writeOnly": True})
    # Empty means "use the provider's default base URL".
    llm_base_url: str = ""
    # Empty means "do not send reasoning_effort". Any short token is accepted
    # and forwarded; the server decides whether it understands it.
    llm_reasoning_effort: str = Field(default="", pattern=r"^[A-Za-z0-9_-]{0,32}$")
    composio_api_key: str = Field(default="", json_schema_extra={"writeOnly": True})
    llm_api_key_configured: bool = False
    composio_api_key_configured: bool = False
    default_model: str = Field(default_factory=lambda: settings.DEFAULT_MODEL)
    theme: str = "dark"
    # Connected apps whose tools are withheld from every bot (still connected).
    disabled_toolkits: List[str] = Field(default_factory=list)
    # Auto-approval of actions that need approval, scored by a decision model.
    # off: never consulted; shadow: scored and logged while the user still
    # decides; on: actions scored below the threshold run without asking.
    auto_approval: Literal["off", "shadow", "on"] = Field(
        default_factory=lambda: settings.AUTO_APPROVAL if settings.AUTO_APPROVAL in {"off", "shadow", "on"} else "off"
    )
    # Base URL of a server with POST /v1/systemone, e.g. a halogen-flash-server.
    decider_url: str = Field(
        default_factory=lambda: settings.DECIDER_URL,
        max_length=300,
        pattern=r"^(https?://[^\s]+)?$",
    )
    # An action is auto-approved only when every risk score is below this.
    decider_threshold: float = Field(default_factory=lambda: settings.DECIDER_THRESHOLD, ge=0.01, le=0.99)
    # Requests in flight at the model server at once, shared fairly between
    # accounts; the rest wait in the chat with their place shown. 0 = no limit.
    llm_max_concurrent: int = Field(default_factory=lambda: settings.LLM_MAX_CONCURRENT_REQUESTS, ge=0, le=64)
    # Screenshots to the model are scaled to this width when wider; 0 = the full screen.
    computer_screenshot_max_width: int = Field(default_factory=lambda: settings.COMPUTER_SCREENSHOT_MAX_WIDTH, ge=0, le=4096)
    # Retrieval over long attachments: embeddings and a reranker on the model server.
    retrieval_enabled: Literal["on", "off"] = Field(default_factory=lambda: "on" if settings.RETRIEVAL_ENABLED.lower() in {"1", "true", "on", "yes"} else "off")
    embedding_model: str = Field(default_factory=lambda: settings.EMBEDDING_MODEL, max_length=200)
    rerank_model: str = Field(default_factory=lambda: settings.RERANK_MODEL, max_length=200)
    # Narrow the connected apps' tools to the ones that match the message when a turn would carry more than this; 0 = all.
    tool_selection_max: int = Field(default_factory=lambda: settings.TOOL_SELECTION_MAX, ge=0, le=500)

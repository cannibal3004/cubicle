"""Auto-approval of governed actions, scored by a decision model.

Every action that needs approval still goes through the action gateway and
the approval broker. This gate sits beside the approval card: given an open
`ActionRequest`, it asks a decision model (decider-4b on a halogen-flash-
server NPU, or anything serving `POST /v1/systemone`) a few yes/no risk
questions about the proposed action and, when every answer is confidently
"no", answers the approval on the user's behalf. Anything else is left to the
user exactly as before.

It fails closed: an unreachable server, a malformed reply, a timeout, or an
unknown action all mean "ask the user". Every scored action is recorded in
`gate_decisions` together with what was finally decided, so the log doubles
as labelled data for tuning the questions or the model later.
"""

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

from app.config import settings
from app.schemas.contracts import ActionRequest
from app.services.action_gateway import redact_sensitive
from app.services.npu import NpuError, post_json_with_retry
from app.services.storage_service import StorageService, current_storage
from app.services.workspace_service import WorkspaceService, WorkspaceToolError, workspace_service

logger = logging.getLogger(__name__)

MODES = ("off", "shadow", "on")

# The yes/no risk questions. The decider answers each as its own pass over the
# same state, so adding a question costs one NPU pass per action. Keep each
# one a plain question with no rules in it: the model follows described
# options far better than instructions.
# Each question states its direction ("as opposed to only downloading", "as opposed to
# only reading"): without that, decider-4b read "send data to a network service" as "uses
# the network" and "touch git history" literally, so docs fetches and `git log` were held.
# Likewise "replace a file's contents" held every `cat > file <<EOF` edit a coding bot
# makes, and "change something other people can see" held local `git commit`s.
QUESTIONS: Dict[str, str] = {
    "destroy": "Does this action permanently delete or discard files, directories, or data, as opposed to creating or editing project files?",
    "exfiltrate": "Does this action upload or transmit the contents of local files, credentials, or private data to a remote host, as opposed to only downloading from one?",
    "privileged": "Does this action access credentials or secrets, or change system settings, permissions, git remotes, or shared git history, as opposed to only reading project files or git history?",
    "remote_code": "Does this action download and execute code from the internet?",
    "irreversible": "Does this action send a message, make a payment, publish or post something, or change anything outside this computer that other people can see?",
}
# The state the model sees. Only the action and the user's latest request go in;
# never tool output or file contents, which could carry injected instructions.
MAX_FIELD_CHARS = 2000

# Rules decided without the model. The model cannot tell a wholesale rewrite from a
# wipe when all it sees is a write, so a write that leaves an existing file at a
# quarter of its size or less is always held. Pointer and keyboard events give the
# model nothing to judge (a click is a pair of coordinates), so they are not scored.
SHRINK_RATIO = 0.25
UNSCORED_ACTIONS = frozenset({"computer.send_input"})


@dataclass
class GateVerdict:
    """What the gate concluded about one action."""

    mode: str                                   # off | shadow | on
    outcome: str                                # auto_approved | ask | skipped | error
    reason: str = ""
    scores: Dict[str, float] = field(default_factory=dict)
    max_score: Optional[float] = None
    threshold: Optional[float] = None
    latency_ms: Optional[int] = None
    state: Optional[Dict[str, Any]] = None
    rule: Optional[str] = None                  # set when a rule, not the model, decided

    @property
    def auto_approved(self) -> bool:
        return self.outcome == "auto_approved"

    @property
    def decided_by(self) -> str:
        return "rule" if self.rule else "decider"

    def to_event(self) -> Dict[str, Any]:
        """The compact form sent to the client and stored with the tool record."""
        return {
            "mode": self.mode,
            "outcome": self.outcome,
            "reason": self.reason,
            "scores": {key: round(value, 3) for key, value in self.scores.items()},
            "maxScore": None if self.max_score is None else round(self.max_score, 3),
            "threshold": self.threshold,
            "rule": self.rule,
        }


class DeciderError(RuntimeError):
    """The decision server could not give a usable answer."""


class DeciderClient:
    """Minimal client for `POST /v1/systemone` (TypeSafe's System One format)."""

    def __init__(self, base_url: str, timeout: float):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def score(self, state: Dict[str, Any], questions: Dict[str, str]) -> Dict[str, float]:
        payload = {
            "state": state,
            "questions": {key: {"type": "noul", "instructions": text} for key, text in questions.items()},
        }
        try:
            response = await post_json_with_retry(f"{self.base_url}/v1/systemone", payload, timeout=httpx.Timeout(self.timeout, connect=5.0), label="decision model")
        except NpuError as exc:
            raise DeciderError(f"decision server unreachable: {exc}") from exc
        if response.status_code != 200:
            detail = ""
            try:
                detail = str((response.json().get("error") or {}).get("message") or "")[:200]
            except (ValueError, AttributeError):
                pass
            raise DeciderError(f"decision server returned {response.status_code}" + (f": {detail}" if detail else ""))
        try:
            answers = response.json()["answers"]
        except (ValueError, KeyError, TypeError) as exc:
            raise DeciderError("decision server reply had no answers") from exc

        scores: Dict[str, float] = {}
        for key in questions:
            answer = answers.get(key) if isinstance(answers, dict) else None
            value = answer.get("noul") if isinstance(answer, dict) else None
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0.0 <= float(value) <= 1.0:
                raise DeciderError(f"decision server gave no probability for {key}")
            scores[key] = float(value)
        return scores


def _clip(value: Any, limit: int = MAX_FIELD_CHARS) -> Any:
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + f"... [{len(value) - limit} more characters]"
    if isinstance(value, dict):
        return {str(key): _clip(item, limit) for key, item in value.items()}
    if isinstance(value, list):
        return [_clip(item, limit) for item in value[:50]]
    return value


class ApprovalGate:
    def __init__(
        self,
        storage: Optional[StorageService] = None,
        workspace: WorkspaceService = workspace_service,
        client_factory=DeciderClient,
    ):
        self._storage = storage
        self.workspace = workspace
        self.client_factory = client_factory

    @property
    def storage(self) -> StorageService:
        # The caller's own settings and decisions unless a store was pinned.
        return self._storage or current_storage()

    # ---- configuration ------------------------------------------------------

    def mode_for(self, bot: Optional[Dict[str, Any]]) -> str:
        """The effective mode for a bot: its own override, else the app setting."""
        override = (bot or {}).get("auto_approval")
        if override in MODES:
            return override
        app = self.storage.get_settings().get("auto_approval")
        return app if app in MODES else "off"

    def threshold_for(self, bot: Optional[Dict[str, Any]]) -> float:
        """The effective risk threshold for a bot: its own override, else the app setting."""
        override = (bot or {}).get("gate_threshold")
        if isinstance(override, (int, float)) and not isinstance(override, bool) and 0.01 <= float(override) <= 0.99:
            return float(override)
        return self._config()["threshold"]

    def _config(self) -> Dict[str, Any]:
        current = self.storage.get_settings()
        try:
            threshold = float(current.get("decider_threshold") or settings.DECIDER_THRESHOLD)
        except (TypeError, ValueError):
            threshold = settings.DECIDER_THRESHOLD
        return {
            "url": str(current.get("decider_url") or settings.DECIDER_URL or "").strip(),
            "threshold": min(0.99, max(0.01, threshold)),
        }

    # ---- what the model is shown ---------------------------------------------

    def build_state(self, request: ActionRequest, call: Any, user_request: str) -> Dict[str, Any]:
        """The action as the decision model sees it: keyed, compact, no tool output.

        `arguments` are the display arguments the approval card shows; for a
        workspace write that is the path and byte count, never the content.
        Facts the harness knows for sure (does the file exist, how big is it)
        are added as plain keys: the model reads keyed lookups far better than
        it infers them.
        """
        # The request's arguments, target and preview were redacted when the action
        # was opened; the user's own message was not, and a pasted key must not
        # reach the model or the decision log either.
        state: Dict[str, Any] = {
            "user_request": _clip(redact_sensitive(user_request or "")),
            "tool": f"{request.tool}.{request.action}",
            "intent": request.intent,
            "arguments": _clip(dict(request.arguments)),
        }
        if request.target:
            state["target"] = _clip(dict(request.target))
        if request.tool == "workspace" and request.action in ("write", "delete"):
            path = str(request.arguments.get("path") or getattr(call, "path", "") or "")
            existing = self.workspace.existing_size(path) if path else None
            state["file_exists"] = existing is not None
            if existing is not None:
                state["existing_bytes"] = existing
        return state

    # ---- scoring ---------------------------------------------------------------

    async def evaluate(
        self,
        request: ActionRequest,
        call: Any,
        *,
        mode: str,
        user_request: str,
        threshold: Optional[float] = None,
    ) -> GateVerdict:
        """Score one open action. Never raises; failures become an `error` verdict."""
        if mode not in ("shadow", "on"):
            return GateVerdict(mode=mode, outcome="skipped", reason="auto-approval is off")
        if not request.requires_approval:
            return GateVerdict(mode=mode, outcome="skipped", reason="action does not need approval")
        config = self._config()
        if threshold is not None:
            config["threshold"] = min(0.99, max(0.01, float(threshold)))
        state = self.build_state(request, call, user_request)
        ruled = self._apply_rules(request, state, mode=mode, threshold=config["threshold"])
        if ruled is not None:
            return ruled
        if not config["url"]:
            return GateVerdict(mode=mode, outcome="error", reason="no decision server configured")

        started = time.monotonic()
        try:
            scores = await self.client_factory(config["url"], settings.DECIDER_TIMEOUT_SECONDS).score(state, QUESTIONS)
        except DeciderError as exc:
            verdict = GateVerdict(mode=mode, outcome="error", reason=str(exc), threshold=config["threshold"], state=state)
            verdict.latency_ms = int((time.monotonic() - started) * 1000)
            logger.warning("Auto-approval gate could not score %s: %s", request.request_id, exc)
            return verdict
        latency_ms = int((time.monotonic() - started) * 1000)

        top = max(scores, key=scores.get)
        max_score = scores[top]
        if max_score < config["threshold"]:
            outcome = "auto_approved" if mode == "on" else "ask"
            reason = f"all risk scores below {config['threshold']:g}" + (" (shadow: the user still decides)" if mode == "shadow" else "")
        else:
            outcome = "ask"
            reason = f"{top} scored {max_score:.2f}, at or above {config['threshold']:g}"
        return GateVerdict(
            mode=mode,
            outcome=outcome,
            reason=reason,
            scores=scores,
            max_score=max_score,
            threshold=config["threshold"],
            latency_ms=latency_ms,
            state=state,
        )

    def _apply_rules(self, request: ActionRequest, state: Dict[str, Any], *, mode: str, threshold: float) -> Optional[GateVerdict]:
        """Verdicts that need no model: a shrinking write is held, input events pass."""
        name = f"{request.tool}.{request.action}"
        arguments = state.get("arguments") if isinstance(state.get("arguments"), dict) else {}
        existing = state.get("existing_bytes")
        new_bytes = arguments.get("bytes")
        if (
            name == "workspace.write"
            and isinstance(existing, int) and existing > 0
            and isinstance(new_bytes, int) and not isinstance(new_bytes, bool)
            and new_bytes <= existing * SHRINK_RATIO
        ):
            return GateVerdict(
                mode=mode,
                outcome="ask",
                reason=f"write shrinks {arguments.get('path', '')} from {existing} to {new_bytes} bytes",
                rule="shrinking_write",
                threshold=threshold,
                state=state,
            )
        if name in UNSCORED_ACTIONS:
            return GateVerdict(
                mode=mode,
                outcome="auto_approved" if mode == "on" else "ask",
                reason="input events are not scored" + (" (shadow: the user still decides)" if mode == "shadow" else ""),
                rule="input_event",
                threshold=threshold,
                state=state,
            )
        return None

    # ---- the decision log --------------------------------------------------------

    def record(self, request: ActionRequest, verdict: GateVerdict) -> None:
        self.storage.add_gate_decision(
            {
                "request_id": request.request_id,
                "thread_id": request.thread_id,
                "bot_id": request.bot_id,
                "tool": f"{request.tool}.{request.action}",
                "preview": request.preview,
                "risk": request.risk,
                "mode": verdict.mode,
                "outcome": verdict.outcome,
                "reason": verdict.reason,
                "scores": verdict.scores,
                "max_score": verdict.max_score,
                "threshold": verdict.threshold,
                "rule": verdict.rule,
                "latency_ms": verdict.latency_ms,
                "state": verdict.state,
                "questions": QUESTIONS if verdict.scores else None,
                "final_decision": None,
                "decided_by": None,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )

    def record_final(self, request_id: str, decision: str, decided_by: str) -> None:
        """What was finally decided (allow/deny/expired) and by whom (user/decider)."""
        self.storage.update_gate_decision(
            request_id,
            {"final_decision": decision, "decided_by": decided_by, "resolved_at": datetime.now(timezone.utc).isoformat()},
        )

    async def check(self, base_url: str) -> Dict[str, Any]:
        """Send one fixed, harmless probe to a decision server and report what came back."""
        client = self.client_factory(base_url, settings.DECIDER_TIMEOUT_SECONDS)
        state = {"user_request": "list the files", "tool": "workspace.list", "arguments": {"path": "."}}
        started = time.monotonic()
        try:
            scores = await client.score(state, QUESTIONS)
        except DeciderError as exc:
            return {"ok": False, "error": str(exc), "latency_ms": int((time.monotonic() - started) * 1000)}
        return {"ok": True, "scores": {key: round(value, 3) for key, value in scores.items()}, "latency_ms": int((time.monotonic() - started) * 1000)}


approval_gate = ApprovalGate()

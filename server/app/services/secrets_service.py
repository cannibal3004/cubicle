"""Named secrets for bot computers: API keys and tokens the user stores once.

Two scopes: app secrets reach every bot's computer, a bot's own secrets add
to or override them. Values are encrypted at rest with the same key as the
provider credentials, are never returned by the API after they are set, and
reach a bot only as environment variables inside its sandbox. Because a
shell can still print them, every tool result a bot sees is passed through
`Redactor`, which replaces each known value with `[secret NAME]`.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, List, Optional

from app.services.storage_service import StorageService, storage_service

NAME_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
MAX_VALUE_CHARS = 8192
# Values shorter than this are not redacted: a two-letter "secret" would
# turn every tool result into confetti.
MIN_REDACT_CHARS = 4
# Names the sandbox needs for itself; a secret may not take them over.
RESERVED_NAMES = {
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "LANG", "DISPLAY", "PORT", "WORKSPACE",
    "COMPUTER_TOKEN", "COMPUTER_ID", "CUBICLE_PACKAGES", "CHROME_CDP_PORT",
    "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "GSETTINGS_BACKEND",
    "VIEWPORT_WIDTH", "VIEWPORT_HEIGHT", "LD_PRELOAD", "LD_LIBRARY_PATH", "NODE_OPTIONS", "PYTHONPATH",
    "PYTHONSTARTUP", "BASH_ENV", "ENV", "PROMPT_COMMAND", "DEBIAN_FRONTEND",
}


class SecretError(ValueError):
    pass


def validate_name(name: Any) -> str:
    cleaned = name.strip().upper() if isinstance(name, str) else ""
    if not NAME_PATTERN.match(cleaned):
        raise SecretError("A secret name is 1 to 64 characters: letters, digits and underscores, starting with a letter (it becomes an environment variable).")
    if cleaned in RESERVED_NAMES:
        raise SecretError(f"{cleaned} is used by the sandbox itself; choose another name.")
    return cleaned


def validate_value(value: Any) -> str:
    if not isinstance(value, str):
        raise SecretError("A secret value must be text.")
    cleaned = value.strip()
    if not cleaned:
        raise SecretError("A secret value cannot be empty.")
    if len(cleaned) > MAX_VALUE_CHARS:
        raise SecretError(f"A secret value is limited to {MAX_VALUE_CHARS} characters.")
    if "\x00" in cleaned:
        raise SecretError("A secret value cannot contain NUL characters.")
    return cleaned


def _storage(storage: Optional[StorageService]) -> StorageService:
    return storage or storage_service


def list_app_secrets(storage: Optional[StorageService] = None) -> List[Dict[str, Any]]:
    return [{**entry, "source": "app"} for entry in _storage(storage).list_secrets("app")]


def list_for_bot(bot_id: str, storage: Optional[StorageService] = None) -> List[Dict[str, Any]]:
    """App secrets and the bot's own, with the bot's own on top of same-named app ones."""
    store = _storage(storage)
    own = {entry["name"]: entry for entry in store.list_secrets("bot", bot_id)}
    merged: Dict[str, Dict[str, Any]] = {}
    for entry in store.list_secrets("app"):
        merged[entry["name"]] = {**entry, "source": "app", "overridden": entry["name"] in own}
    for name, entry in own.items():
        merged[name] = {**entry, "source": "bot", "overridden": False, "overrides_app": name in merged}
    return [merged[name] for name in sorted(merged)]


def set_secret(scope: str, bot_id: str, name: Any, value: Any, storage: Optional[StorageService] = None) -> Dict[str, Any]:
    if scope not in ("app", "bot"):
        raise SecretError("Unknown secret scope.")
    entry = _storage(storage).set_secret(scope, bot_id if scope == "bot" else "", validate_name(name), validate_value(value))
    return {**entry, "source": scope}


def delete_secret(scope: str, bot_id: str, name: Any, storage: Optional[StorageService] = None) -> bool:
    return _storage(storage).delete_secret(scope, bot_id if scope == "bot" else "", validate_name(name))


def environment_for(bot_id: str, storage: Optional[StorageService] = None) -> Dict[str, str]:
    """What a bot's computer gets as environment variables: app secrets, then its own."""
    return {name: value for name, value in _storage(storage).secret_values(bot_id).items() if name not in RESERVED_NAMES}


def names_for(bot_id: str, storage: Optional[StorageService] = None) -> List[str]:
    return sorted(environment_for(bot_id, storage))


class Redactor:
    """Replace known secret values in text (and nested payloads) with `[secret NAME]`."""

    def __init__(self, values: Dict[str, str]):
        pairs = []
        for name, value in values.items():
            if not isinstance(value, str) or len(value) < MIN_REDACT_CHARS:
                continue
            pairs.append((value, name))
            escaped = json.dumps(value)[1:-1]
            if escaped != value:
                # The value as it appears inside a JSON-encoded tool result.
                pairs.append((escaped, name))
        # Longest first, so a value that contains another is replaced whole.
        self._pairs = sorted(pairs, key=lambda pair: len(pair[0]), reverse=True)

    def __bool__(self) -> bool:
        return bool(self._pairs)

    def __call__(self, text: Any) -> Any:
        if not isinstance(text, str) or not self._pairs:
            return text
        for value, name in self._pairs:
            if value in text:
                text = text.replace(value, f"[secret {name}]")
        return text

    def apply(self, payload: Any) -> Any:
        if not self._pairs:
            return payload
        if isinstance(payload, str):
            return self(payload)
        if isinstance(payload, dict):
            return {key: self.apply(value) for key, value in payload.items()}
        if isinstance(payload, (list, tuple)):
            return [self.apply(item) for item in payload]
        return payload


def redactor_for(bot_id: str, storage: Optional[StorageService] = None) -> Redactor:
    return Redactor(_storage(storage).secret_values(bot_id))


EnvironmentProvider = Callable[[str], Dict[str, str]]

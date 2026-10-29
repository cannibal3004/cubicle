"""A bot's projects: named pieces of work, each with work notes and a log.

Notes come in two tiers. The bot's notes (bot_memory) hold durable facts
about its job, shared by every conversation. Work notes hold where a piece
of work stands: status, decisions in flight, next steps. They belong to the
project a conversation is in, or to the conversation itself when it is in
none. History goes to the project's log, which stays out of the context:
entries are stored here, indexed for retrieval, and come back when a message
matches them (and on request with project_log_read).

Work notes reach the model the way the bot's notes do, without touching the
system prompt: a conversation that starts in a project (or joins one) gets
a note row with them, a change made in another conversation of the same
project is announced once, and a compaction carries them in its summary.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.config import settings
from app.services.action_gateway import ActionDefinition, ActionGateway, ActionInvocation, action_gateway
from app.services.identity import get_current_owner, owner_context
from app.services.storage_service import StorageService, current_storage

logger = logging.getLogger(__name__)

WORK_NOTES_MAX_CHARS = settings.BOT_WORK_NOTES_MAX_CHARS
NAME_MAX_CHARS = 80
LOG_ENTRY_MAX_CHARS = 4000
RECENT_LOG = 8
WORK_NOTES_ITEM_TYPE = "work_notes"
LOG_KIND = "project_log"
ID_PREFIX = "pj-"


class ProjectError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now().isoformat()


# ---- projects ------------------------------------------------------------------------------


def clean_name(name: Any) -> str:
    cleaned = " ".join(str(name or "").split())[:NAME_MAX_CHARS]
    if not cleaned:
        raise ProjectError("A project needs a name.")
    return cleaned


def clean_work_notes(text: Any) -> str:
    if text is None:
        text = ""
    if not isinstance(text, str):
        raise ProjectError("Work notes must be text.")
    cleaned = text.replace("\r\n", "\n").strip()
    if len(cleaned) > WORK_NOTES_MAX_CHARS:
        raise ProjectError(
            f"Work notes are limited to {WORK_NOTES_MAX_CHARS} characters; this would make them {len(cleaned)}. They say where the work "
            "stands now: move what happened along the way to the project log (project_log), and durable facts about your job to your "
            "notes (memory_edit), then try again."
        )
    return cleaned


# Each project has a folder in the bot's computer for its files, /workspace/projects/<folder>.
# The name is chosen once, from the project's name, and kept when the project is renamed.
FOLDER_ROOT = "projects"
FOLDER_MAX_CHARS = 40


def _folder_name(name: str, taken: set) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")[:FOLDER_MAX_CHARS].strip("-") or "project"
    folder, n = base, 2
    while folder in taken:
        folder, n = f"{base}-{n}", n + 1
    return folder


def folder_of(project: Dict[str, Any], storage: Optional[StorageService] = None) -> str:
    """The project's folder name (projects made before folders get one now)."""
    if project.get("folder"):
        return str(project["folder"])
    storage = storage or current_storage()
    taken = {str(p.get("folder")) for p in list_for_bot(project["bot_id"], storage) if p.get("folder")}
    folder = _folder_name(str(project.get("name") or ""), taken)
    storage.save_project({**project, "folder": folder})
    project["folder"] = folder
    return folder


def workspace_path(project: Dict[str, Any], storage: Optional[StorageService] = None) -> str:
    return f"/workspace/{FOLDER_ROOT}/{folder_of(project, storage)}"


def list_for_bot(bot_id: str, storage: Optional[StorageService] = None) -> List[Dict[str, Any]]:
    storage = storage or current_storage()
    return storage.get_projects(bot_id)


def get(project_id: str, storage: Optional[StorageService] = None) -> Optional[Dict[str, Any]]:
    return (storage or current_storage()).get_project(project_id)


def find(bot_id: str, name: str, storage: Optional[StorageService] = None, *, include_archived: bool = True) -> Optional[Dict[str, Any]]:
    """The bot's project with this name (any case) or id."""
    wanted = " ".join(str(name or "").split()).casefold()
    if not wanted:
        return None
    for project in list_for_bot(bot_id, storage):
        if (project.get("archived") and not include_archived):
            continue
        if project["id"] == name or str(project.get("name") or "").casefold() == wanted:
            return project
    return None


def create(bot_id: str, name: str, notes: str = "", storage: Optional[StorageService] = None) -> Dict[str, Any]:
    storage = storage or current_storage()
    if not any(b.get("id") == bot_id for b in storage.get_bots()):
        raise ProjectError("Bot not found")
    name = clean_name(name)
    if find(bot_id, name, storage, include_archived=False):
        raise ProjectError(f"There is already a project called {name!r}.")
    now = _now()
    taken = {str(p.get("folder")) for p in list_for_bot(bot_id, storage) if p.get("folder")}
    record = {"id": f"{ID_PREFIX}{uuid.uuid4().hex[:12]}", "bot_id": bot_id, "name": name, "notes": clean_work_notes(notes),
              "created_at": now, "updated_at": now, "notes_updated_at": now if notes else None, "archived": False,
              "folder": _folder_name(name, taken)}
    storage.save_project(record)
    return record


def update(project_id: str, changes: Dict[str, Any], storage: Optional[StorageService] = None) -> Dict[str, Any]:
    storage = storage or current_storage()
    record = storage.get_project(project_id)
    if record is None:
        raise ProjectError("That project does not exist.")
    if "name" in changes:
        name = clean_name(changes["name"])
        other = find(record["bot_id"], name, storage, include_archived=False)
        if other and other["id"] != project_id:
            raise ProjectError(f"There is already a project called {name!r}.")
        changes = {**changes, "name": name}
    record = {**record, **changes, "updated_at": _now()}
    storage.save_project(record)
    return record


def conversations_in(project_id: str, storage: Optional[StorageService] = None) -> List[Dict[str, Any]]:
    storage = storage or current_storage()
    project = storage.get_project(project_id)
    if project is None:
        return []
    return [c for c in storage.get_conversations(project["bot_id"]) if c.get("project_id") == project_id]


def delete(project_id: str, storage: Optional[StorageService] = None) -> int:
    """Delete a project and its log. Its conversations stay, outside any project."""
    storage = storage or current_storage()
    project = storage.get_project(project_id)
    if project is None:
        return 0
    moved = 0
    for record in conversations_in(project_id, storage):
        join(record["id"], None, storage)
        moved += 1
    try:
        from app.services import retrieval

        index = retrieval.index_for(storage)
        for entry in storage.get_log(project_id):
            index.delete(LOG_KIND, f"{project_id}:{entry['id']}")
    except Exception as exc:  # retrieval is a nicety
        logger.warning("Could not drop the log index of %s: %s", project_id, exc)
    storage.delete_project(project_id)
    return moved


# ---- which work notes a conversation has ---------------------------------------------------


def scope_of(thread_id: str, storage: Optional[StorageService] = None, project_name: Optional[str] = None) -> Dict[str, Any]:
    """Whose work notes a call means: a named project, the conversation's project, or the conversation itself."""
    from app.services import conversations

    storage = storage or current_storage()
    conversation = conversations.get(thread_id, storage)
    if conversation is None:
        raise ProjectError("The conversation does not exist.")
    if project_name:
        project = find(conversation["bot_id"], project_name, storage)
        if project is None:
            raise ProjectError(f"There is no project called {project_name!r}; projects_list shows them, project_create makes one.")
        return {"kind": "project", "project": project, "conversation": conversation}
    project = storage.get_project(conversation.get("project_id") or "") if conversation.get("project_id") else None
    if project is not None:
        return {"kind": "project", "project": project, "conversation": conversation}
    return {"kind": "conversation", "project": None, "conversation": conversation}


def notes_of(scope: Dict[str, Any]) -> str:
    if scope["kind"] == "project":
        return str(scope["project"].get("notes") or "")
    return str(scope["conversation"].get("work_notes") or "")


def _label(scope: Dict[str, Any]) -> str:
    return f"project {scope['project']['name']!r}" if scope["kind"] == "project" else "this conversation (it is in no project)"


def save_notes(scope: Dict[str, Any], text: str, storage: StorageService, *, from_thread: Optional[str]) -> Dict[str, Any]:
    """Store work notes where they belong; the project's other conversations are told once."""
    from app.services import conversations

    now = _now()
    if scope["kind"] == "project":
        project = update(scope["project"]["id"], {"notes": text, "notes_updated_at": now}, storage)
        told = announce(project, storage, except_thread=from_thread)
        return {"where": _label(scope), "chars": len(text), "updated_at": now, "conversations_told": told}
    conversations.update(scope["conversation"]["id"], {"work_notes": text, "work_notes_updated_at": now}, storage)
    return {"where": _label(scope), "chars": len(text), "updated_at": now}


def announce(project: Dict[str, Any], storage: StorageService, *, except_thread: Optional[str]) -> int:
    """One notice in each other conversation of the project that its work notes changed, until it reads them."""
    from app.services import conversations

    activity = storage.thread_activity()
    told = 0
    for record in conversations_in(project["id"], storage):
        if record["id"] == except_thread or record.get("archived") or record.get("work_notes_stale"):
            continue
        if not (activity.get(record["id"]) or {}).get("messages"):
            continue
        storage.add_message(work_note_message(record["id"], "", bot_id=project["bot_id"], by="elsewhere", project_name=project["name"]))
        conversations.update(record["id"], {"work_notes_stale": True}, storage)
        told += 1
    return told


def seen(thread_id: Optional[str], storage: Optional[StorageService] = None) -> None:
    """The conversation has the work notes as they are now."""
    from app.services import conversations

    if not thread_id:
        return
    storage = storage or current_storage()
    record = storage.get_conversation(thread_id)
    if record and record.get("work_notes_stale"):
        conversations.update(thread_id, {"work_notes_stale": False}, storage)


def work_note_message(thread_id: str, text: str, *, bot_id: str, by: str, project_name: Optional[str] = None, message_id: Optional[str] = None) -> Dict[str, Any]:
    """A thread item about work notes: the ones a conversation starts with or gets on joining a project
    ("start", "joined"), leaving one ("left"), the user's edit ("user"), the state after an undo
    ("rewind"), or word that another conversation of the project changed them ("elsewhere")."""
    return {
        "id": message_id or f"msg-{uuid.uuid4().hex[:6]}",
        "thread_id": thread_id,
        "bot_id": bot_id,
        "sender": "system",
        "text": text,
        "created_at": _now(),
        "item_type": WORK_NOTES_ITEM_TYPE,
        "raw_payload": {"by": by, "project": project_name, "chars": len(text)},
    }


def join(thread_id: str, project_id: Optional[str], storage: Optional[StorageService] = None) -> Dict[str, Any]:
    """Put a conversation in a project (None: take it out), telling the bot in the conversation."""
    from app.services import conversations

    storage = storage or current_storage()
    record = conversations.get(thread_id, storage)
    if record is None:
        raise ProjectError("The conversation does not exist.")
    previous = storage.get_project(record.get("project_id") or "") if record.get("project_id") else None
    project = storage.get_project(project_id) if project_id else None
    if project_id and (project is None or project["bot_id"] != record["bot_id"]):
        raise ProjectError("That project does not exist.")
    if (previous or {}).get("id") == (project or {}).get("id"):
        return record
    record = conversations.update(thread_id, {"project_id": project["id"] if project else None, "work_notes_stale": False}, storage)
    if storage.get_messages(thread_id=thread_id):
        if project is not None:
            storage.add_message(work_note_message(thread_id, str(project.get("notes") or ""), bot_id=record["bot_id"], by="joined", project_name=project["name"]))
        elif previous is not None:
            storage.add_message(work_note_message(thread_id, str(record.get("work_notes") or ""), bot_id=record["bot_id"], by="left", project_name=previous["name"]))
    return record


# ---- the log -------------------------------------------------------------------------------


def add_log(project: Dict[str, Any], text: Any, *, thread_id: Optional[str] = None, storage: Optional[StorageService] = None) -> Dict[str, Any]:
    storage = storage or current_storage()
    if not isinstance(text, str) or not text.strip():
        raise ProjectError("A log entry needs text.")
    text = text.replace("\r\n", "\n").strip()
    if len(text) > LOG_ENTRY_MAX_CHARS:
        raise ProjectError(f"A log entry is limited to {LOG_ENTRY_MAX_CHARS} characters; this one is {len(text)}. Split it, or keep the detail in a file.")
    entry = {"project_id": project["id"], "thread_id": thread_id, "created_at": _now(), "text": text}
    entry["id"] = storage.add_log_entry(project["id"], entry)
    update(project["id"], {"log_updated_at": entry["created_at"]}, storage)
    _schedule_index(project, entry)
    return entry


def render_log(entries: List[Dict[str, Any]]) -> str:
    if not entries:
        return "(no entries yet)"
    return "\n".join(f"--- {str(e.get('created_at') or '')[:16].replace('T', ' ')} ---\n{e.get('text', '')}" for e in entries)


def _schedule_index(project: Dict[str, Any], entry: Dict[str, Any]) -> None:
    from app.services import retrieval

    try:
        config = retrieval.current_config()
    except Exception:
        return
    if not config.enabled:
        return
    owner = get_current_owner()
    when = str(entry.get("created_at") or "")[:10]

    async def run() -> None:
        with owner_context(owner):
            try:
                await retrieval.index_text(
                    LOG_KIND, f"{project['id']}:{entry['id']}", entry["text"],
                    meta={"name": f"the log of {project['name']}, {when}", "bot_id": project["bot_id"]}, config=config,
                )
            except Exception as exc:  # the entry itself is stored
                logger.warning("Could not index a log entry of %s: %s", project["id"], exc)

    try:
        asyncio.get_running_loop().create_task(run())
    except RuntimeError:
        pass


async def log_context(project_id: str, query: str, storage: Optional[StorageService] = None) -> str:
    """Log entries of the project that match a message, as a block for the turn; "" when none."""
    from app.services import retrieval

    try:
        config = retrieval.current_config()
        if not config.enabled:
            return ""
        hits = await retrieval.retrieve(query, kind=LOG_KIND, ref_prefix=f"{project_id}:", config=config, storage=storage, relevant_only=True)
    except Exception as exc:  # never let it fail a turn
        logger.warning("Project log retrieval failed for %s: %s", project_id, exc)
        return ""
    return retrieval.render_hits(hits, heading="Entries from the project log that match this message:")


# ---- the bot's tools -------------------------------------------------------------------------


def _thread(call: ActionInvocation) -> str:
    thread_id = call.arguments.get("thread_id") or call.arguments.get("bot_id")
    if not isinstance(thread_id, str) or not thread_id:
        raise ProjectError("Project actions need a conversation.")
    return thread_id


def _summary(project: Dict[str, Any], storage: StorageService, current: Optional[str] = None) -> Dict[str, Any]:
    notes = str(project.get("notes") or "")
    first_line = next((line.strip() for line in notes.splitlines() if line.strip()), "")
    return {
        "name": project["name"],
        "archived": bool(project.get("archived")),
        "status": first_line[:200],
        "work_notes_chars": len(notes),
        "log_entries": storage.count_log(project["id"]),
        "conversations": len(conversations_in(project["id"], storage)),
        "folder": workspace_path(project, storage),
        **({"this_conversation": True} if current and project["id"] == current else {}),
    }


async def execute_list(call: ActionInvocation) -> Dict[str, Any]:
    from app.services import conversations

    storage = current_storage()
    record = conversations.get(_thread(call), storage) or {}
    bot_id = record.get("bot_id") or call.arguments.get("bot_id")
    listed = [_summary(p, storage, record.get("project_id")) for p in list_for_bot(bot_id, storage)]
    return {"projects": listed, "this_conversation_is_in": next((p["name"] for p in listed if p.get("this_conversation")), None)}


async def execute_create(call: ActionInvocation) -> Dict[str, Any]:
    from app.services import conversations

    storage = current_storage()
    record = conversations.get(_thread(call), storage) or {}
    project = create(record.get("bot_id") or call.arguments.get("bot_id"), call.arguments.get("name"), call.arguments.get("work_notes") or "", storage)
    return {"created": project["name"], "work_notes_chars": len(project["notes"]), "folder": workspace_path(project, storage),
            "note": "This conversation is not in it; project_join moves it there. Keep the project's files in its folder."}


async def execute_join(call: ActionInvocation) -> Dict[str, Any]:
    from app.services import conversations

    storage = current_storage()
    thread_id = _thread(call)
    record = conversations.get(thread_id, storage) or {}
    name = str(call.arguments.get("name") or "").strip()
    if not name:
        join(thread_id, None, storage)
        return {"project": None, "note": "This conversation is in no project now; its work notes are its own again.",
                "work_notes": str(record.get("work_notes") or "")}
    project = find(record.get("bot_id"), name, storage)
    if project is None:
        raise ProjectError(f"There is no project called {name!r}; projects_list shows them, project_create makes one.")
    if project.get("archived"):
        update(project["id"], {"archived": False}, storage)
    join(thread_id, project["id"], storage)
    seen(thread_id, storage)
    return {"project": project["name"], "folder": workspace_path(project, storage), "work_notes": str(project.get("notes") or ""),
            "recent_log": render_log(storage.get_log(project["id"], limit=RECENT_LOG))}


async def execute_read(call: ActionInvocation) -> Dict[str, Any]:
    storage = current_storage()
    scope = scope_of(_thread(call), storage, call.arguments.get("project") or None)
    if not call.arguments.get("project"):
        seen(_thread(call), storage)
    text = notes_of(scope)
    return {"where": _label(scope), "work_notes": text, "chars": len(text), "limit": WORK_NOTES_MAX_CHARS}


async def execute_write(call: ActionInvocation) -> Dict[str, Any]:
    storage = current_storage()
    thread_id = _thread(call)
    scope = scope_of(thread_id, storage, call.arguments.get("project") or None)
    text = clean_work_notes(call.arguments.get("text"))
    result = save_notes(scope, text, storage, from_thread=thread_id)
    seen(thread_id, storage)
    return {**result, "limit": WORK_NOTES_MAX_CHARS}


async def execute_edit(call: ActionInvocation) -> Dict[str, Any]:
    storage = current_storage()
    thread_id = _thread(call)
    scope = scope_of(thread_id, storage, call.arguments.get("project") or None)
    old_text = str(call.arguments.get("old_text") or "").replace("\r\n", "\n")
    new_text = str(call.arguments.get("new_text") or "").replace("\r\n", "\n")
    if not old_text.strip():
        raise ProjectError("'old_text' must be the exact passage to replace.")
    current = notes_of(scope)
    count = current.count(old_text)
    if count == 0:
        raise ProjectError("The passage to replace is not in the work notes. Copy it exactly (work_notes_read shows them) or rewrite with work_notes_write.")
    if count > 1:
        raise ProjectError(f"The passage to replace appears {count} times; include more of the surrounding text so it matches once.")
    updated = clean_work_notes(current.replace(old_text, new_text, 1))
    result = save_notes(scope, updated, storage, from_thread=thread_id)
    seen(thread_id, storage)
    return {**result, "limit": WORK_NOTES_MAX_CHARS}


async def execute_log(call: ActionInvocation) -> Dict[str, Any]:
    storage = current_storage()
    thread_id = _thread(call)
    scope = scope_of(thread_id, storage, call.arguments.get("project") or None)
    if scope["kind"] != "project":
        raise ProjectError(
            "This conversation is in no project, so there is no log to add to. Join one with project_join (project_create makes one), "
            "or name the project with 'project'."
        )
    entry = add_log(scope["project"], call.arguments.get("entry"), thread_id=thread_id, storage=storage)
    return {"logged_to": scope["project"]["name"], "at": entry["created_at"], "entries": storage.count_log(scope["project"]["id"])}


async def execute_log_read(call: ActionInvocation) -> Dict[str, Any]:
    from app.services import retrieval

    storage = current_storage()
    scope = scope_of(_thread(call), storage, call.arguments.get("project") or None)
    if scope["kind"] != "project":
        raise ProjectError("This conversation is in no project, so it has no log. Name a project with 'project'.")
    project = scope["project"]
    query = str(call.arguments.get("query") or "").strip()
    if query:
        hits = await retrieval.retrieve(query, kind=LOG_KIND, ref_prefix=f"{project['id']}:", storage=storage)
        if hits:
            return {"project": project["name"], "matching": retrieval.render_hits(hits, heading=f"Log entries of {project['name']} that match {query!r}:")}
        return {"project": project["name"], "matching": "", "note": "Nothing in the log matches; the newest entries follow.",
                "recent": render_log(storage.get_log(project["id"], limit=RECENT_LOG))}
    try:
        limit = max(1, min(50, int(call.arguments.get("limit") or RECENT_LOG)))
    except (TypeError, ValueError):
        limit = RECENT_LOG
    return {"project": project["name"], "entries": storage.count_log(project["id"]), "recent": render_log(storage.get_log(project["id"], limit=limit))}


ACTIONS = [
    ("projects.list", "projects", "list", "List the bot's projects.", "read", execute_list),
    ("projects.create", "projects", "create", "Create a project for the bot.", "write", execute_create),
    ("projects.join", "projects", "join", "Put the conversation in a project, or take it out.", "write", execute_join),
    ("work_notes.read", "work_notes", "read", "Show the bot its work notes.", "read", execute_read),
    ("work_notes.write", "work_notes", "write", "Replace the bot's work notes.", "write", execute_write),
    ("work_notes.edit", "work_notes", "edit", "Change one passage of the bot's work notes.", "write", execute_edit),
    ("project_log.add", "project_log", "add", "Add an entry to a project's log.", "write", execute_log),
    ("project_log.read", "project_log", "read", "Read or search a project's log.", "read", execute_log_read),
]


def register_project_actions(gateway: Optional[ActionGateway] = None) -> None:
    target = gateway or action_gateway
    for name, tool, action, intent, risk, executor in ACTIONS:
        target.register_action(ActionDefinition(name=name, tool=tool, action=action, intent=intent, risk=risk, requires_approval=False), executor)


register_project_actions()

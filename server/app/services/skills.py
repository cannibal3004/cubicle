"""Skills: instructions for a kind of task, with any scripts, templates and references they need,
loaded by a bot when a task fits.

The format is the open Agent Skills one: a folder whose SKILL.md starts with YAML front matter
giving a `name` and a `description`, followed by the instructions; other files in the folder
(scripts/, references, templates) are read or run only when the instructions call for them.

There are two kinds:

- The library. Installed from a zip, a GitHub folder or repository, or a link to a zip; or written
  in App Settings. System skills (admins) reach every account's bots, an account's own skills
  its own bots. Each lives as a folder under DATA_DIR/skills/<system|owner id>/<skill id>/, with
  an index in the settings store (`skills`, `user_skills`). Every bot gets every library skill
  unless it is switched off in the bot's settings (`skill_settings` {id: false}).
- A bot's own skills: what it wrote itself with skill_save, most often after the user showed it a
  task (these were called procedures, and are still kept on the bot record under `procedures`,
  see services/procedures).

Every turn's tools prompt lists the skills by name and description; skill_read brings one back
in full, and copies its files into the bot's computer (/workspace/skills/<folder>) so its
scripts can run there.
"""

from __future__ import annotations

import io
import logging
import re
import shutil
import stat
import time
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import yaml

from app.services.action_gateway import ActionDefinition, ActionGateway, ActionInvocation, action_gateway
from app.services.storage_service import StorageService, current_storage

logger = logging.getLogger(__name__)

SKILL_FILE = "SKILL.md"
SCOPES = ("system", "user")
SCOPE_KEYS = {"system": "skills", "user": "user_skills"}
NAME_MAX = 64
DESCRIPTION_MAX = 1024
INSTRUCTIONS_MAX = 200_000
MAX_SKILLS = 100  # per scope
MAX_FILES = 300  # per skill
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_SKILL_BYTES = 25 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 60 * 1024 * 1024
READ_FILE_MAX_CHARS = 60_000
PROMPT_DESCRIPTION_CHARS = 300
PROMPT_MAX_SKILLS = 60
# Where a skill's files go in the bot's computer, and the marker that says which version is there.
COMPUTER_FOLDER = "skills"
_MARKER = ".cubicle-skill"
_ID = re.compile(r"^skill-[a-f0-9]{8}$")
_SPACES = re.compile(r"\s+")
_GITHUB = re.compile(r"^https://github\.com/([^/\s]+)/([^/\s]+?)(?:\.git)?(?:/(tree|blob)/([^/\s]+)(?:/(.*))?)?/?$")


class SkillError(RuntimeError):
    pass


# ---- SKILL.md -------------------------------------------------------------------------------


def parse_skill_md(text: str) -> Tuple[Dict[str, Any], str]:
    """The front matter (a dict) and the instructions after it."""
    text = text.replace("\r\n", "\n").lstrip("﻿")
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            try:
                meta = yaml.safe_load(text[4:end]) or {}
            except yaml.YAMLError as exc:
                # Hand-written front matter often breaks YAML with a colon in a description
                # ("Checks the backups: snapshot age, locks"); read it as key: value lines.
                meta = _plain_front_matter(text[4:end])
                if not meta:
                    raise SkillError(f"SKILL.md's front matter is not valid YAML: {exc}") from exc
            if not isinstance(meta, dict):
                raise SkillError("SKILL.md's front matter must be a mapping (name: ..., description: ...).")
            body = text[end + 4:]
            return meta, body.split("\n", 1)[1].lstrip("\n") if "\n" in body else ""
    return {}, text


def _plain_front_matter(text: str) -> Dict[str, Any]:
    """Top-level `key: value` lines, the value being the rest of the line (quotes dropped)."""
    meta: Dict[str, Any] = {}
    for line in text.splitlines():
        match = re.match(r"^([A-Za-z][A-Za-z0-9_-]*):\s*(.*)$", line)
        if match:
            meta[match.group(1)] = match.group(2).strip().strip("'\"")
    return meta if meta.get("name") or meta.get("description") else {}


def compose_skill_md(meta: Dict[str, Any], instructions: str) -> str:
    front = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, width=1000).strip()
    return f"---\n{front}\n---\n\n{instructions.strip()}\n"


def clean_name(value: Any) -> str:
    name = _SPACES.sub(" ", str(value or "")).strip()
    if not name:
        raise SkillError("A skill needs a name.")
    if len(name) > NAME_MAX:
        raise SkillError(f"A skill's name is at most {NAME_MAX} characters.")
    return name


def clean_description(value: Any) -> str:
    description = _SPACES.sub(" ", str(value or "")).strip()
    if not description:
        raise SkillError("A skill needs a description: what it does and when to use it (the bot decides from this).")
    return description[:DESCRIPTION_MAX]


def folder_name(name: str) -> str:
    """The skill's folder in the bot's computer: its name in lower case, hyphenated."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:NAME_MAX] or "skill"


def _first_line(instructions: str) -> str:
    for line in instructions.splitlines():
        text = line.strip().lstrip("#").strip()
        if text:
            return text
    return ""


# ---- reading a skill out of an archive or a set of files --------------------------------------


def _safe_relative(name: str) -> Optional[str]:
    """An archive member's path, or None when it would leave the folder (or is not a plain path)."""
    path = PurePosixPath(name.replace("\\", "/"))
    if path.is_absolute() or not path.parts or any(part in ("", ".", "..") for part in path.parts):
        return None
    return str(path)


def files_from_zip(data: bytes, *, strip_top: bool = False, within: str = "") -> Dict[str, bytes]:
    """The files of a zip, by path; `strip_top` drops the one top folder (GitHub's archives), `within`
    keeps only what is under that folder (relative to it). Links and paths that leave are skipped."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise SkillError("That is not a zip file.") from exc
    files: Dict[str, bytes] = {}
    total = 0
    prefix = within.strip("/") + "/" if within.strip("/") else ""
    for info in archive.infolist():
        if info.is_dir() or stat.S_ISLNK(info.external_attr >> 16):
            continue
        path = _safe_relative(info.filename)
        if path is None:
            continue
        if strip_top:
            parts = path.split("/", 1)
            if len(parts) < 2:
                continue
            path = parts[1]
        if prefix:
            if not path.startswith(prefix):
                continue
            path = path[len(prefix):]
        if not path or "__MACOSX" in path.split("/"):
            continue
        if info.file_size > MAX_FILE_BYTES:
            raise SkillError(f"{path} is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB.")
        total += info.file_size
        if total > MAX_DOWNLOAD_BYTES:
            raise SkillError("The archive is too large to unpack.")
        files[path] = archive.read(info)
    if within and not files:
        raise SkillError(f"Nothing under {within} in that archive.")
    return files


def split_skills(files: Dict[str, bytes]) -> List[Tuple[str, Dict[str, bytes]]]:
    """Each skill in a set of files: (its folder, its files relative to that folder). A skill is a
    folder with a SKILL.md; one inside another belongs to the outer one."""
    roots = sorted({str(PurePosixPath(path).parent) for path in files if PurePosixPath(path).name.lower() == "skill.md"}, key=len)
    outer: List[str] = []
    for root in roots:
        if not any(root == o or root.startswith(o + "/") or o == "." for o in outer):
            outer.append(root)
    skills = []
    for root in outer:
        prefix = "" if root == "." else root + "/"
        members = {path[len(prefix):]: data for path, data in files.items() if path.startswith(prefix)}
        skills.append((root, members))
    if not skills:
        raise SkillError("No SKILL.md found: a skill is a folder with a SKILL.md in it.")
    return skills


def describe_files(files: Dict[str, bytes]) -> Tuple[Dict[str, Any], str, List[Dict[str, Any]]]:
    """A skill's front matter, instructions and file list, checked against the limits."""
    skill_md = next((path for path in files if path.lower() == "skill.md"), None)
    if skill_md is None:
        raise SkillError("No SKILL.md at the top of the skill's folder.")
    if len(files) > MAX_FILES:
        raise SkillError(f"A skill has at most {MAX_FILES} files; this one has {len(files)}.")
    if sum(len(data) for data in files.values()) > MAX_SKILL_BYTES:
        raise SkillError(f"A skill is at most {MAX_SKILL_BYTES // (1024 * 1024)} MB.")
    try:
        text = files[skill_md].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SkillError("SKILL.md is not UTF-8 text.") from exc
    meta, instructions = parse_skill_md(text)
    if len(instructions) > INSTRUCTIONS_MAX:
        raise SkillError(f"SKILL.md's instructions are at most {INSTRUCTIONS_MAX} characters.")
    listing = [{"path": path, "size": len(data)} for path, data in sorted(files.items()) if path != skill_md]
    return meta, instructions, listing


# ---- the library ----------------------------------------------------------------------------


class SkillStore:
    """The skills of one scope: `system` (admins; every account's bots) or `user` (the current
    account's own). The index lives in the settings store, the folders on disk."""

    def __init__(self, storage: Optional[StorageService] = None, scope: str = "user"):
        if scope not in SCOPES:
            raise SkillError("scope must be system or user.")
        self._storage = storage
        self.scope = scope
        self.key = SCOPE_KEYS[scope]

    @property
    def storage(self) -> StorageService:
        return self._storage or current_storage()

    @property
    def root(self) -> Path:
        owner = "system" if self.scope == "system" else re.sub(r"[^A-Za-z0-9_.-]+", "-", self.storage.owner_id)
        return Path(self.storage.data_dir) / "skills" / owner

    def folder(self, record: Dict[str, Any]) -> Path:
        return self.root / record["id"]

    def list(self) -> List[Dict[str, Any]]:
        raw = self.storage.get_settings().get(self.key) or []
        return [{**item, "scope": self.scope} for item in raw if isinstance(item, dict) and _ID.fullmatch(str(item.get("id") or ""))] if isinstance(raw, list) else []

    def get(self, skill_id: str) -> Dict[str, Any]:
        for item in self.list():
            if item["id"] == skill_id:
                return item
        raise SkillError("There is no skill with that id.")

    def _save(self, skills: List[Dict[str, Any]]) -> None:
        self.storage.save_settings({self.key: [{k: v for k, v in s.items() if k != "scope"} for s in skills]})

    def _write_folder(self, record: Dict[str, Any], files: Dict[str, bytes]) -> None:
        """The skill's files, swapped in whole (a reinstall leaves nothing of the old version)."""
        target = self.folder(record)
        staging = target.with_name(f"{record['id']}.new-{uuid.uuid4().hex[:6]}")
        try:
            for path, data in files.items():
                destination = staging / path
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(data)
            if target.exists():
                shutil.rmtree(target)
            staging.rename(target)
        except OSError as exc:
            shutil.rmtree(staging, ignore_errors=True)
            raise SkillError(f"Could not store the skill's files: {exc}") from exc

    def install(self, files: Dict[str, bytes], *, source: Dict[str, Any], fallback_name: str = "") -> Dict[str, Any]:
        """Add a skill from its files, or replace the one with the same name."""
        meta, instructions, listing = describe_files(files)
        name = clean_name(meta.get("name") or fallback_name)
        description = clean_description(meta.get("description") or _first_line(instructions))
        skills = self.list()
        existing = next((s for s in skills if s["name"].lower() == name.lower()), None)
        if existing is None and len(skills) >= MAX_SKILLS:
            raise SkillError(f"At most {MAX_SKILLS} skills; delete one first.")
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        record = {
            "id": existing["id"] if existing else f"skill-{uuid.uuid4().hex[:8]}",
            "name": name,
            "description": description,
            "files": listing,
            "size": sum(len(data) for data in files.values()),
            "source": {k: v for k, v in source.items() if v},
            "created_at": existing.get("created_at", now) if existing else now,
            "updated_at": now,
            # Changes with every write: the copy in a bot's computer is replaced when it differs.
            "version": uuid.uuid4().hex[:8],
        }
        self._write_folder(record, files)
        skills = [s for s in skills if s["id"] != record["id"]] + [record]
        self._save(skills)
        return {**record, "scope": self.scope}

    def install_all(self, files: Dict[str, bytes], *, source: Dict[str, Any]) -> List[Dict[str, Any]]:
        found = split_skills(files)
        if len(found) > 50:
            raise SkillError(f"That holds {len(found)} skills; install at most 50 at once (link to the folder of the one you want).")
        installed = []
        for root, members in found:
            fallback = PurePosixPath(root).name if root != "." else (source.get("name") or "")
            installed.append(self.install(members, source={**source, "path": source.get("path") or (root if root != "." else "")}, fallback_name=fallback))
        return installed

    def create(self, name: Any, description: Any, instructions: Any) -> Dict[str, Any]:
        """A skill written in the app: a SKILL.md and nothing else."""
        name, description = clean_name(name), clean_description(description)
        text = str(instructions or "").strip()
        if not text:
            raise SkillError("A skill needs instructions.")
        if any(s["name"].lower() == name.lower() for s in self.list()):
            raise SkillError(f"A skill named {name} already exists.")
        return self.install({SKILL_FILE: compose_skill_md({"name": name, "description": description}, text).encode("utf-8")}, source={"kind": "written"})

    def update(self, skill_id: str, *, name: Any = None, description: Any = None, instructions: Any = None) -> Dict[str, Any]:
        """Change the name, description or instructions; the other files stay as they are."""
        record = self.get(skill_id)
        meta, body = parse_skill_md(self.skill_md(record))
        new_name = clean_name(name) if name is not None else record["name"]
        if new_name.lower() != record["name"].lower() and any(s["name"].lower() == new_name.lower() for s in self.list()):
            raise SkillError(f"A skill named {new_name} already exists.")
        meta = {**meta, "name": new_name, "description": clean_description(description) if description is not None else record["description"]}
        if instructions is not None:
            body = str(instructions).strip()
            if not body:
                raise SkillError("A skill needs instructions.")
        files = self.files_of(record)
        skill_md = next((path for path in files if path.lower() == "skill.md"), SKILL_FILE)
        files[skill_md] = compose_skill_md(meta, body).encode("utf-8")
        skills = [s for s in self.list() if s["id"] != skill_id]
        _, _, listing = describe_files(files)
        updated = {**{k: v for k, v in record.items() if k != "scope"}, "name": new_name, "description": meta["description"], "files": listing,
                   "size": sum(len(d) for d in files.values()), "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "version": uuid.uuid4().hex[:8]}
        self._write_folder(updated, files)
        self._save(skills + [updated])
        return {**updated, "scope": self.scope}

    def remove(self, skill_id: str) -> Dict[str, Any]:
        record = self.get(skill_id)
        self._save([s for s in self.list() if s["id"] != skill_id])
        shutil.rmtree(self.folder(record), ignore_errors=True)
        return record

    def files_of(self, record: Dict[str, Any]) -> Dict[str, bytes]:
        folder = self.folder(record)
        return {str(path.relative_to(folder).as_posix()): path.read_bytes() for path in folder.rglob("*") if path.is_file()} if folder.exists() else {}

    def skill_md(self, record: Dict[str, Any]) -> str:
        folder = self.folder(record)
        path = next((p for p in (folder / SKILL_FILE, folder / "skill.md") if p.is_file()), None)
        if path is None:
            raise SkillError(f"The files of the skill {record['name']} are missing; install it again.")
        return path.read_text(encoding="utf-8", errors="replace")

    def instructions(self, record: Dict[str, Any]) -> str:
        return parse_skill_md(self.skill_md(record))[1].strip()

    def read_file(self, record: Dict[str, Any], path: str) -> str:
        folder = self.folder(record).resolve()
        relative = _safe_relative(str(path or "").strip().lstrip("/"))
        target = (folder / relative).resolve() if relative else None
        if target is None or folder not in target.parents or not target.is_file():
            names = ", ".join(f["path"] for f in record.get("files") or []) or "none"
            raise SkillError(f"The skill {record['name']} has no file {path!r}. Its files: {names}.")
        data = target.read_bytes()
        if b"\0" in data[:4096]:
            raise SkillError(f"{path} is not a text file; use it from the skill's folder in your computer.")
        text = data.decode("utf-8", errors="replace")
        if len(text) > READ_FILE_MAX_CHARS:
            text = text[:READ_FILE_MAX_CHARS] + f"\n[... cut at {READ_FILE_MAX_CHARS} characters of {len(text)}]"
        return text


system_skill_store = SkillStore(scope="system")
user_skill_store = SkillStore(scope="user")


def store_for(record: Dict[str, Any]) -> SkillStore:
    return system_skill_store if record.get("scope") == "system" else user_skill_store


def library() -> List[Dict[str, Any]]:
    """System skills, then the current account's own."""
    return system_skill_store.list() + user_skill_store.list()


def find(skill_id: str) -> Dict[str, Any]:
    for record in library():
        if record["id"] == skill_id:
            return record
    raise SkillError("There is no skill with that id.")


# ---- installing from a link ---------------------------------------------------------------------


def source_for(url: str) -> Dict[str, Any]:
    """What a link points at: a GitHub repository or folder (as an archive to fetch and the folder in
    it), a zip, or a single SKILL.md."""
    url = str(url or "").strip()
    match = _GITHUB.match(url)
    if match:
        owner, repo, kind, ref, path = match.groups()
        path = (path or "").strip("/")
        if kind == "blob":
            if PurePosixPath(path).name.lower() != "skill.md":
                raise SkillError("Link to a skill's folder (or its SKILL.md), not to another file.")
            path = str(PurePosixPath(path).parent).strip(".")
        return {"kind": "github", "archive": f"https://github.com/{owner}/{repo}/archive/{ref or 'HEAD'}.zip", "within": path, "url": url, "name": PurePosixPath(path).name or repo}
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise SkillError("Give an http(s) link: a GitHub repository or folder, a .zip, or a SKILL.md.")
    if parsed.path.lower().endswith(".zip"):
        return {"kind": "zip", "archive": url, "within": "", "url": url, "name": PurePosixPath(parsed.path).stem}
    if PurePosixPath(parsed.path).name.lower() == "skill.md":
        return {"kind": "file", "file": url, "url": url, "name": PurePosixPath(parsed.path).parent.name}
    raise SkillError("That link is not a GitHub repository or folder, a .zip, or a SKILL.md.")


async def _download(url: str) -> bytes:
    import httpx

    async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(60, connect=15)) as client:
        async with client.stream("GET", url, headers={"User-Agent": "Cubicle skills"}) as response:
            if response.status_code == 404:
                raise SkillError(f"Not found: {url} (a private repository cannot be fetched).")
            if response.status_code >= 400:
                raise SkillError(f"Fetching {url} failed: HTTP {response.status_code}.")
            chunks, size = [], 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > MAX_DOWNLOAD_BYTES:
                    raise SkillError(f"{url} is larger than {MAX_DOWNLOAD_BYTES // (1024 * 1024)} MB.")
                chunks.append(chunk)
    return b"".join(chunks)


async def install_from_url(url: str, store: SkillStore) -> List[Dict[str, Any]]:
    source = source_for(url)
    origin = {"kind": "link", "url": source["url"], "name": source["name"]}
    if source["kind"] == "file":
        return [store.install({SKILL_FILE: await _download(source["file"])}, source=origin, fallback_name=source["name"])]
    data = await _download(source["archive"])
    files = files_from_zip(data, strip_top=source["kind"] == "github", within=source["within"])
    return store.install_all(files, source={**origin, "path": source["within"]})


def install_upload(filename: str, data: bytes, store: SkillStore) -> List[Dict[str, Any]]:
    """A zip of one or more skill folders, or a lone SKILL.md."""
    name = PurePosixPath(str(filename or "")).name
    if name.lower().endswith(".md"):
        return [store.install({SKILL_FILE: data}, source={"kind": "upload", "name": name})]
    files = files_from_zip(data)
    # A zip of one folder holding the skill: that folder is the skill.
    return store.install_all(files, source={"kind": "upload", "name": name})


# ---- a bot's skills -------------------------------------------------------------------------------


def bot_library(bot: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The library skills this bot has: all of them, less the ones switched off in its settings."""
    off = {k for k, v in ((bot or {}).get("skill_settings") or {}).items() if v is False}
    return [record for record in library() if record["id"] not in off]


def own_skills(bot: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [p for p in ((bot or {}).get("procedures") or []) if isinstance(p, dict) and p.get("title")]


def prompt_block(bot: Optional[Dict[str, Any]]) -> str:
    """The skills, by name and description, for the tools prompt; "" when the bot has none."""
    lines: List[str] = []
    shared = bot_library(bot)
    if shared:
        lines.append(
            f"- Skills ({len(shared)}): instructions for particular kinds of tasks, with any scripts and files they need. When a task fits "
            "one, read it with skill_read first and follow it:"
        )
        for record in shared[:PROMPT_MAX_SKILLS]:
            description = record.get("description") or ""
            if len(description) > PROMPT_DESCRIPTION_CHARS:
                description = description[:PROMPT_DESCRIPTION_CHARS].rstrip() + "…"
            lines.append(f"  - {record['name']}: {description}")
    own = own_skills(bot)
    if own:
        titles = "; ".join(f'"{p["title"]}"' + (f" ({p['description']})" if p.get("description") else "") for p in own[:PROMPT_MAX_SKILLS])
        lines.append(
            f"- Skills you wrote ({len(own)}), most from what the user showed you: {titles}. When a task matches one, read it with skill_read "
            "first and follow it; when you learn that a step has changed, save the corrected skill under the same name with skill_save."
        )
    return "\n".join(lines)


def _match(items: List[Dict[str, Any]], wanted: str, key: str) -> Optional[Dict[str, Any]]:
    exact = [item for item in items if str(item.get(key) or "").lower() == wanted]
    if exact:
        return exact[0]
    partial = [item for item in items if wanted in str(item.get(key) or "").lower()]
    return partial[0] if len(partial) == 1 else None


def copy_to_computer(record: Dict[str, Any], bot_id: str) -> Optional[str]:
    """The skill's files in the bot's computer (copied when missing or out of date); the folder's
    path there, or None when the computer's files cannot be reached from here."""
    from app.services.file_share import FileShareError, source_root

    try:
        root = source_root("computer", bot_id)
    except FileShareError:
        return None
    if not root.is_dir():
        return None
    relative = f"{COMPUTER_FOLDER}/{folder_name(record['name'])}"
    target = root / relative
    stamp = f"{record['id']} {record.get('version') or record.get('updated_at') or ''}"
    try:
        if (target / _MARKER).is_file() and (target / _MARKER).read_text(encoding="utf-8").strip() == stamp:
            return f"/workspace/{relative}"
        # A new version replaces the folder whole, so nothing of the old one lingers.
        if target.exists():
            shutil.rmtree(target)
        for path, data in store_for(record).files_of(record).items():
            destination = target / path
            _make_open_folders(root, destination.parent)
            destination.write_bytes(data)
            # The computer runs as another user than the API: what goes there must be open to it.
            destination.chmod(0o777 if path.startswith("scripts/") or path.endswith((".sh", ".py")) else 0o666)
        _make_open_folders(root, target)
        (target / _MARKER).write_text(stamp, encoding="utf-8")
    except OSError as exc:
        logger.info("Skill %s not copied to %s's computer: %s", record["name"], bot_id, exc)
        return None
    return f"/workspace/{relative}"


def _make_open_folders(root: Path, folder: Path) -> None:
    """`folder` and the ones above it up to `root`, each open to the computer's user."""
    missing = []
    current = folder
    while current != root and not current.exists():
        missing.append(current)
        current = current.parent
    for path in reversed(missing):
        path.mkdir()
        path.chmod(0o777)


def _bot(bot_id: str) -> Dict[str, Any]:
    bot = next((b for b in current_storage().get_bots() if b.get("id") == bot_id), None)
    if bot is None:
        raise SkillError("The bot does not exist.")
    return bot


async def execute_read(call: ActionInvocation) -> Dict[str, Any]:
    bot_id = str(call.arguments.get("bot_id") or "")
    bot = _bot(bot_id)
    wanted = _SPACES.sub(" ", str(call.arguments.get("name") or "")).strip().lower()
    if not wanted:
        raise SkillError("Name the skill to read.")
    shared = bot_library(bot)
    record = _match(shared, wanted, "name")
    if record is not None:
        store = store_for(record)
        file = str(call.arguments.get("file") or "").strip()
        if file:
            return {"skill": record["name"], "file": file, "text": store.read_file(record, file)}
        result: Dict[str, Any] = {"skill": record["name"], "description": record.get("description"), "instructions": store.instructions(record)}
        if record.get("files"):
            folder = copy_to_computer(record, bot_id)
            result["files"] = [f["path"] for f in record["files"]]
            if folder:
                result["folder"] = folder
                result["note"] = f"The skill's files are in your computer at {folder}; paths in the instructions are relative to it. Run its scripts from there."
            else:
                result["note"] = "Your computer's files cannot be reached from here: read a text file with skill_read(name, file=...)."
        return result
    own = _match(own_skills(bot), wanted, "title")
    if own is not None:
        return {"skill": own["title"], "description": own.get("description") or "", "instructions": own["steps"], "yours": True, "updated_at": own.get("updated_at")}
    names = [r["name"] for r in shared] + [p["title"] for p in own_skills(bot)]
    raise SkillError(f"No skill called {call.arguments.get('name')!r}. Your skills: {names}" if names else "You have no skills yet.")


async def execute_save(call: ActionInvocation) -> Dict[str, Any]:
    from app.services import procedures

    bot_id = str(call.arguments.get("bot_id") or "")
    try:
        saved = procedures.save_procedure(
            bot_id, call.arguments.get("name"), call.arguments.get("instructions"),
            description=call.arguments.get("description"), source=call.arguments.get("source"),
        )
    except procedures.ProcedureError as exc:
        raise SkillError(str(exc)) from exc
    return {"saved": True, "name": saved["title"], "chars": len(saved["steps"]), "count": len(procedures.list_procedures(bot_id))}


READ_ACTION = "skill.read"
SAVE_ACTION = "skill.save"


def register_skill_actions(gateway: Optional[ActionGateway] = None) -> None:
    target = gateway or action_gateway
    target.register_action(
        ActionDefinition(name=READ_ACTION, tool="skill", action="read", intent="Show the bot one of its skills.", risk="read", requires_approval=False),
        execute_read,
    )
    target.register_action(
        ActionDefinition(name=SAVE_ACTION, tool="skill", action="save", intent="Keep a skill the bot wrote.", risk="write", requires_approval=False),
        execute_save,
    )


register_skill_actions()

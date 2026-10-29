"""Made-up data for the docs screenshots. Runs inside the docs stack's api container
(tools/docs/screenshots.py pipes it to `python -`) against an empty data volume, and
prints one JSON line: the sign-in tokens and the ids the screenshot script opens.

Nothing here is real: the people, bots, hosts and outputs are invented.
"""

import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from app.config import settings
from app.services import conversations, projects, secrets_service, sharing
from app.services.auth_service import auth_service
from app.services.identity import LOCAL_USER_ID, owner_context
from app.services.schedule_service import create_schedule
from app.services.storage_service import storage_service

NOW = datetime.now().replace(microsecond=0)
OWNER, PASSWORD = "alex", "demo-password-1234"
accounts = auth_service.accounts

# ---- people -------------------------------------------------------------------------------

accounts.complete_setup(OWNER, PASSWORD)
accounts.update_profile(LOCAL_USER_ID, display_name="Alex", email="alex@example.com", about="I run a small home lab and like short, direct answers.")
_, owner_token = accounts.create_token(LOCAL_USER_ID, "docs screenshots")
sam = accounts.create_user("sam", PASSWORD, role="member")
accounts.update_profile(sam["id"], display_name="Sam", email="sam@example.com")
_, sam_token = accounts.create_token(sam["id"], "docs screenshots")

storage = storage_service
storage.save_settings({"retrieval_enabled": "off", "default_model": settings.DEFAULT_MODEL or "qwen3.8-flash-next", "title_model": ""})


def at(minutes_ago: float) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


# ---- replies: rounds of thinking, text and tool calls, stored as a turn stores them ---------

def call(name, arguments, summary, result=None, *, status="completed", ran_ms=None, error=None, gate=None, trusted_by=None):
    record = {
        "id": f"call_{uuid.uuid4().hex[:16]}",
        "name": name,
        "status": status,
        "raw_arguments": json.dumps(arguments),
        "arguments": arguments,
        "summary": summary,
    }
    if result is not None:
        record["result"] = json.dumps(result, ensure_ascii=False)
    if ran_ms is not None:
        record["ran_ms"] = ran_ms
    if error:
        record["error"] = error
    if gate:
        record["gate"] = gate
    if trusted_by:
        record.update({"trusted": True, "trusted_by": trusted_by})
    return record


def reply(thread_id, bot_id, minutes_ago, rounds, *, thinking=None, usage=None):
    pieces, reasoning, parts, records = [], "", [], []
    for round_ in rounds:
        position = len("\n\n".join(pieces))
        if round_.get("thinking"):
            parts.append({"at": position, "after": len(records), "start": len(reasoning)})
            reasoning += round_["thinking"].strip() + "\n"
        if round_.get("text"):
            pieces.append(round_["text"].strip())
        calls_at = len("\n\n".join(pieces))
        for record in round_.get("calls", []):
            records.append({**record, "at": calls_at})
    payload = {}
    if reasoning:
        payload.update({"reasoning": reasoning, "reasoning_parts": parts})
    if records:
        payload["tool_calls"] = records
    payload["usage"] = usage or {"prompt_tokens": 9800, "completion_tokens": 620, "last_completion_tokens": 340, "rounds": max(1, len(rounds)), "context_window": 262144}
    payload["thinking"] = thinking or {"level": "xhigh", "auto": False, "source": "app", "sent": "xhigh"}
    storage.add_message({"id": new_id("msg"), "thread_id": thread_id, "bot_id": bot_id, "sender": "bot", "text": "\n\n".join(pieces), "raw_payload": payload, "created_at": at(minutes_ago)})


def say(thread_id, bot_id, minutes_ago, text, **payload):
    storage.add_message({"id": new_id("msg"), "thread_id": thread_id, "bot_id": bot_id, "sender": "user", "text": text, "created_at": at(minutes_ago), **({"raw_payload": payload} if payload else {})})


AUTO = {"outcome": "auto_approved", "mode": "on", "reason": "all risk scores below 0.2", "scores": {"destructive": 0.04, "exfiltration": 0.02}}


def terminal(command, stdout, *, exit_code=0, stderr="", ran_ms=900, **kwargs):
    return call(
        "computer_terminal_execute",
        {"command": command, "timeout_seconds": 120},
        f"Run (up to 2 min): {command}",
        {"stdout": stdout, "stderr": stderr, "exit_code": exit_code},
        ran_ms=ran_ms,
        **kwargs,
    )


# ---- bots -----------------------------------------------------------------------------------

bots = storage.get_bots()  # a fresh account starts with Cubie
cubie = next(b for b in bots if b.get("orchestrator"))


def bot(name, role, avatar, accent, prompt, **extra):
    record = {
        "id": new_id("bot"), "name": name, "role": role, "description": role, "avatar": avatar, "model": settings.DEFAULT_MODEL or "qwen3.8-flash-next",
        "accent_color": accent, "system_prompt": prompt, "tools": [], "tool_settings": {}, "auto_approval": "inherit", "gate_threshold": None,
        "trusted_tools": [], "allowed_commands": [], "orchestrator": False, "suggestions": [], "thinking": "", "pinned": False, "archived": False,
        "unread_count": 0, "created_at": at(60 * 24 * 12),
    }
    record.update(extra)
    return record


atlas = bot(
    "Atlas", "Home lab admin", "🛠️", "#10b981",
    "You are Atlas, who looks after Alex's home lab: a NAS, a mini PC running Docker, and the network. Check before you change things, "
    "and say what you changed.",
    auto_approval="on",
    allowed_commands=["systemctl status", "df -h", "journalctl", "docker ps"],
    trusted_tools=["todo_add", "todo_update", "project_log", "work_notes_write"],
    thinking="auto",
    memory=(
        "## The lab\n- NAS: nas.lan (TrueNAS), pool `tank`, backups land in tank/backups.\n- Docker host: mini.lan, compose files in /opt/stacks.\n"
        "- Backups: restic, nightly at 02:00 via restic-backup.service on mini.lan.\n\n## House rules\n- Alex approves anything that deletes data.\n"
        "- Prefer `systemctl status` before restarting a service."
    ),
    procedures=[
        {"id": "proc-media", "title": "Restart the media server cleanly", "steps": "1. docker compose -f /opt/stacks/media/compose.yml stop jellyfin\n2. Wait for transcodes to stop (docker logs --tail 20 jellyfin)\n3. docker compose -f /opt/stacks/media/compose.yml up -d jellyfin\n4. Check http://mini.lan:8096 answers.", "created_at": at(60 * 24 * 6), "updated_at": at(60 * 24 * 6)},
        {"id": "proc-snap", "title": "Check the NAS snapshot schedule", "steps": "1. Open https://nas.lan > Data Protection > Periodic Snapshot Tasks\n2. tank/backups should keep 14 daily snapshots.", "created_at": at(60 * 24 * 3), "updated_at": at(60 * 24 * 3)},
    ],
    suggestions=["Is everything on the NAS healthy?", "How much disk space is left?", "Did last night's backup run?"],
)
iris = bot(
    "Iris", "Research assistant", "🔭", "#8b5cf6",
    "You are Iris, a careful research assistant. Find sources, compare them, and say how sure you are.",
    suggestions=["Compare three options for me", "Summarise this article", "What changed in the latest release?"],
)
quill = bot(
    "Quill", "Writing partner", "✍️", "#f59e0b",
    "You are Quill. Help Alex write clearly: plain words, short sentences, no filler.",
    suggestions=["Tighten this paragraph", "Draft a polite reply to an email", "Turn my notes into a short update"],
)
storage.save_bots([cubie, atlas, iris, quill])

# ---- Atlas: the backups project and the conversation the hero shot shows --------------------

backup = projects.create(atlas["id"], "Backup overhaul", (
    "Status: nightly restic backup to the NAS works again (stale lock cleared on Oct 29).\n\n"
    "Decisions\n- Keep restic; add an offsite copy to Backblaze B2.\n- Retention: 14 daily, 8 weekly, 6 monthly.\n\n"
    "Next\n1. Create the B2 bucket and keys (Alex).\n2. Add a second restic repository and a weekly copy job.\n3. Restore test from B2."
))
backup_conv = conversations.create(atlas["id"], "Nightly backups failing", project_id=backup["id"])
for minutes, text in ((60 * 24 * 9, "Agreed on the plan: keep restic, add an offsite copy to B2, retention 14 daily / 8 weekly / 6 monthly."),
                      (60 * 24 * 4, "Checked the NAS snapshot task: tank/backups keeps 14 daily snapshots, as planned."),
                      (35, "Nightly backup failed: the repository was locked by a run that died at 02:00 on Oct 28. Removed the stale lock, re-ran the backup by hand, 412 MiB added. Added a lock check to the morning report.")):
    entry = {"project_id": backup["id"], "thread_id": backup_conv["id"], "created_at": at(minutes), "text": text}
    storage.add_log_entry(backup["id"], entry)
projects.update(backup["id"], {"log_updated_at": at(35), "notes_updated_at": at(34)})
conversations.update(backup_conv["id"], {"title_source": "generated", "todo": [
    {"id": "t1", "text": "Find out why last night's backup failed", "state": "done"},
    {"id": "t2", "text": "Clear the stale restic lock and re-run the backup", "state": "done"},
    {"id": "t3", "text": "Add a lock check to the morning report", "state": "done"},
    {"id": "t4", "text": "Set up the offsite copy to B2 once Alex has the keys", "state": "doing"},
    {"id": "t5", "text": "Restore test from B2", "state": "open"},
]})

thread = backup_conv["id"]
say(thread, atlas["id"], 42, "The nightly backup to the NAS failed again last night. Can you find out why and fix it?")
reply(thread, atlas["id"], 38, [
    {"thinking": "Alex says the nightly backup failed. The backup is restic-backup.service on mini.lan, nightly at 02:00. First look at the service's log from last night instead of guessing.",
     "calls": [terminal("journalctl -u restic-backup --since yesterday --no-pager | tail -n 6",
                        "Oct 29 02:00:01 mini systemd[1]: Starting restic-backup.service - Nightly restic backup...\n"
                        "Oct 29 02:00:03 mini restic[48211]: repo already locked, waiting up to 0s for the lock\n"
                        "Oct 29 02:00:03 mini restic[48211]: unable to create lock in backend: repository is already locked by PID 4123 on mini by root (UID 0, GID 0)\n"
                        "Oct 29 02:00:03 mini restic[48211]: lock was created at 2026-10-28 02:00:02 (24h0m1s ago)\n"
                        "Oct 29 02:00:03 mini systemd[1]: restic-backup.service: Main process exited, code=exited, status=1/FAILURE\n"
                        "Oct 29 02:00:03 mini systemd[1]: Failed to start restic-backup.service - Nightly restic backup.",
                        ran_ms=640, trusted_by="allowed_command")]},
    {"thinking": "The repository is locked by PID 4123 from a run 24 hours ago. If that process is gone, the lock is stale and `restic unlock` only removes stale locks, so it is safe. Check the process first.",
     "text": "Last night's run never started: the repository was still locked by the run from the night before.",
     "calls": [terminal("ps -p 4123 -o pid,etime,cmd || echo 'no such process'", "    PID     ELAPSED CMD\nno such process", exit_code=0, ran_ms=120, gate=AUTO),
               terminal("restic -r sftp:backup@nas.lan:/mnt/tank/backups unlock", "successfully removed 1 locks", ran_ms=2310, gate=AUTO)]},
    {"thinking": "The lock is gone. Run the backup now so last night is covered, then confirm it finished.",
     "calls": [terminal("systemctl start restic-backup && systemctl status restic-backup --no-pager | head -n 4",
                        "○ restic-backup.service - Nightly restic backup\n     Loaded: loaded (/etc/systemd/system/restic-backup.service; static)\n"
                        "     Active: inactive (dead) since Wed 2026-10-29 09:14:52 CDT; 2s ago\n   Main PID: 51877 (code=exited, status=0/SUCCESS)",
                        ran_ms=48210, gate=AUTO),
               call("project_log", {"entry": "Nightly backup failed: stale lock from the Oct 28 run. Removed it, re-ran by hand, 412 MiB added."}, "Add to the log of Backup overhaul",
                    {"logged_to": "Backup overhaul", "entries": 3}, ran_ms=18, trusted_by="trusted"),
               call("todo_update", {"id": "t2", "state": "done"}, "Mark a to-do done", {"items": 5}, ran_ms=9, trusted_by="trusted")]},
    {"text": "Fixed. The run on Oct 28 died partway and left its lock behind, so every run since then gave up straight away.\n\n"
             "- **Cleared the stale lock** (the process that held it was long gone).\n"
             "- **Ran the backup by hand**: it finished cleanly and added 412 MiB.\n"
             "- **Logged it** in the Backup overhaul project.\n\n"
             "I'd like to add a lock check to the morning report so this can't go unnoticed for a day again. Shall I?"},
], thinking={"level": "xhigh", "auto": True, "source": "bot", "sent": "medium", "hinted": True}, usage={"prompt_tokens": 14230, "completion_tokens": 1830, "last_completion_tokens": 410, "rounds": 4, "context_window": 262144})
say(thread, atlas["id"], 36, "Yes please, add the lock check.")
reply(thread, atlas["id"], 35, [
    {"thinking": "Quick change: add `restic list locks` to the morning report script.",
     "calls": [call("edit_file", {"path": "/workspace/scripts/morning-report.sh", "old_text": "df -h /mnt/tank", "new_text": "df -h /mnt/tank\necho '--- restic locks'\nrestic -r \"$RESTIC_REPO\" list locks --no-lock"},
                    "Edit /workspace/scripts/morning-report.sh", {"path": "/workspace/scripts/morning-report.sh", "replaced": 1}, ran_ms=31, gate=AUTO)]},
    {"text": "Done: the morning report now lists any restic locks, so a stuck one shows up the next morning."},
], thinking={"level": "low", "auto": True, "source": "bot", "sent": "medium", "hinted": True})

# Each bot's first conversation has the bot's own id; Atlas's is where Alex introduced the lab.
conversations.update(atlas["id"], {"title": "Getting to know the lab", "title_source": "user"})
say(atlas["id"], atlas["id"], 60 * 24 * 11, "Hi Atlas. You look after my home lab: a TrueNAS box (nas.lan) and a mini PC running Docker (mini.lan). Start by noting what's there.")
reply(atlas["id"], atlas["id"], 60 * 24 * 11 - 3, [
    {"calls": [call("memory_write", {"text": "## The lab\n- NAS: nas.lan (TrueNAS)\n- Docker host: mini.lan"}, "Write the notes", {"chars": 64}, ran_ms=11)]},
    {"text": "Noted: the NAS at nas.lan and the Docker host at mini.lan. I'll keep what I learn about them in my notes."},
])
conversations.update(cubie["id"], {"title": "Welcome", "title_source": "user"})

disk = conversations.create(atlas["id"], "Disk space on the NAS")
conversations.update(disk["id"], {"title_source": "generated"})
say(disk["id"], atlas["id"], 60 * 5, "How much space is left on the NAS?")
reply(disk["id"], atlas["id"], 60 * 5 - 1, [
    {"calls": [terminal("ssh nas.lan zpool list -H -o name,size,alloc,free,cap tank", "tank\t21.8T\t14.2T\t7.6T\t65%", ran_ms=850, trusted_by="allowed_command")]},
    {"text": "The `tank` pool is **65% full**: 7.6 TB free of 21.8 TB. At the last month's rate that's well over a year."},
])

weekly = conversations.create(atlas["id"], "Scheduled", purpose="scheduled")
conversations.update(weekly["id"], {"title_source": "fixed"})
say(weekly["id"], atlas["id"], 60 * 26, "Check for updates on the Docker host and list anything with a security fix.", scheduled={"name": "Weekly update check", "at": at(60 * 26)[:16]})
reply(weekly["id"], atlas["id"], 60 * 26 - 2, [
    {"calls": [terminal("docker compose -f /opt/stacks/media/compose.yml pull --dry-run 2>&1 | grep -E 'Pulling|newer' | head", "jellyfin  Pulling\njellyfin  newer image available (10.11.2)", ran_ms=5200, gate=AUTO)]},
    {"text": "One update: **Jellyfin 10.11.2** is out (bug fixes, no security advisories). Nothing else has a newer image."},
])

for name, value in (("B2_ACCOUNT_ID", "made-up-account"), ("B2_APPLICATION_KEY", "made-up-key"), ("RESTIC_PASSWORD", "made-up-password")):
    secrets_service.set_secret("bot", atlas["id"], name, value, storage)
create_schedule(atlas["id"], "Weekly update check", "Check for updates on the Docker host and list anything with a security fix.",
                {"kind": "weekly", "days": ["mon"], "time": "08:00"}, storage=storage)
create_schedule(atlas["id"], "Morning report", "Run the morning report and tell me only if something needs attention.",
                {"kind": "daily", "time": "07:30"}, check={"script": "/workspace/scripts/morning-report.sh --quiet"}, storage=storage)
sharing.add_member(LOCAL_USER_ID, atlas["id"], sam["id"], "member", LOCAL_USER_ID)

# ---- Iris: research with the browser and a file handed over --------------------------------

shared = Path("/workspace/reports")
shared.mkdir(parents=True, exist_ok=True)
(shared / "nas-drives-2026.md").write_text(
    "# NAS drives, October 2026\n\n| Drive | Capacity | Price | Warranty | Notes |\n|---|---|---|---|---|\n"
    "| Seagate IronWolf Pro | 20 TB | $339 | 5 years | Fastest of the three |\n| WD Red Pro | 20 TB | $349 | 5 years | Quietest |\n"
    "| Toshiba N300 Pro | 20 TB | $299 | 5 years | Best price per TB |\n\nAll three are CMR. For a 4-bay NAS that runs all day, any of them is a safe pick.\n",
    encoding="utf-8",
)
drives = conversations.create(iris["id"], "20 TB drives for the NAS")
conversations.update(drives["id"], {"title_source": "generated"})
say(drives["id"], iris["id"], 60 * 3, "I need two more 20 TB drives for the NAS. Compare the usual NAS drives and give me a short table I can keep.")
reply(drives["id"], iris["id"], 60 * 3 - 4, [
    {"thinking": "Three common NAS lines: IronWolf Pro, WD Red Pro, Toshiba N300 Pro. Check current prices and warranty on the makers' and a retailer's pages rather than from memory.",
     "calls": [call("computer_browser_navigate", {"url": "https://www.example-store.com/search?q=20TB+NAS+drive"}, "Open https://www.example-store.com/search?q=20TB+NAS+drive", {"title": "20TB NAS drives", "status": 200}, ran_ms=2400),
               call("computer_screenshot", {}, "Take a screenshot", {"image": "attached as the next message"}, ran_ms=700)]},
    {"thinking": "Prices: IronWolf Pro $339, Red Pro $349, N300 Pro $299. All 5-year warranty, all CMR. Write the table to the shared folder so Alex keeps it.",
     "calls": [call("shared_folder_write", {"path": "reports/nas-drives-2026.md", "content": "# NAS drives, October 2026\n..."}, "Write reports/nas-drives-2026.md in the shared folder", {"path": "reports/nas-drives-2026.md", "bytes": 512}, ran_ms=14),
               call("share_file", {"source": "shared", "path": "reports/nas-drives-2026.md"}, "Hand over reports/nas-drives-2026.md", {"attachment": {"source": "shared", "path": "reports/nas-drives-2026.md"}}, ran_ms=6)]},
    {"text": "All three are CMR drives with a 5-year warranty, so the choice comes down to noise and price:\n\n"
             "| Drive | Price (20 TB) | Notes |\n|---|---|---|\n| Toshiba N300 Pro | $299 | Best price per TB |\n| Seagate IronWolf Pro | $339 | Fastest |\n| WD Red Pro | $349 | Quietest |\n\n"
             "For a NAS in a closet I'd take the **Toshiba N300 Pro**. The table is in your shared folder too."},
], thinking={"level": "xhigh", "auto": False, "source": "app", "sent": "xhigh"})
last = storage.get_messages(drives["id"])[-1]
last["raw_payload"]["attachments"] = [{"source": "shared", "path": "reports/nas-drives-2026.md", "name": "nas-drives-2026.md", "mime": "text/markdown", "size": 512}]
storage.add_message(last)

# ---- Cubie: making a bot and handing it work ------------------------------------------------

garden = conversations.create(cubie["id"], "A bot for the garden sensors")
conversations.update(garden["id"], {"title_source": "generated"})
say(garden["id"], cubie["id"], 90, "I want a bot that keeps an eye on the garden soil sensors and tells me when to water. Can you set that up?")
reply(garden["id"], cubie["id"], 88, [
    {"thinking": "Alex wants a new bot for the soil sensors. Create it with a clear prompt, then hand it a first task so it starts with something useful.",
     "calls": [call("bots_create", {"name": "Fern", "role": "Garden watcher", "avatar": "🌱", "system_prompt": "You are Fern. You watch the garden soil sensors and tell Alex when a bed needs water."},
                    "Create the bot Fern", {"created": "Fern"}, ran_ms=40, gate=AUTO),
               call("bots_send", {"bot": "Fern", "message": "Read the soil moisture sensors (http://garden.lan/api/sensors) and tell Alex which beds are below 30%."},
                    "Send Fern a task", {"sent_to": "Fern", "conversation": "From Cubie"}, ran_ms=25, gate=AUTO)]},
    {"text": "I've made **Fern** 🌱, a garden watcher, and asked it to check the soil sensors now. Its answer will show up here when it's done, and you'll find Fern in your sidebar."},
], thinking={"level": "medium", "auto": False, "source": "app", "sent": "medium"})

# ---- Atlas's computer: its project folder and a page for the browser (only with the real runtime) ----

computer = settings.COMPUTER_PROVIDER == "docker"
if computer:
    from app.services.computer_provider import computer_id_for_bot

    workspace = Path(settings.COMPUTER_DOCKER_WORKSPACE_ROOT) / computer_id_for_bot(atlas["id"])
    files = {
        "projects/backup-overhaul/plan.md": "# Backup overhaul\n\n1. Keep restic, nightly at 02:00.\n2. Offsite copy to Backblaze B2, weekly.\n3. Retention: 14 daily, 8 weekly, 6 monthly.\n",
        "projects/backup-overhaul/retention.txt": "--keep-daily 14 --keep-weekly 8 --keep-monthly 6\n",
        "projects/backup-overhaul/b2-setup.md": "# B2 setup\n\n- Bucket: alex-lab-backups (private)\n- Application key: restic only, this bucket only\n",
        "scripts/morning-report.sh": "#!/bin/sh\n# The morning report: disk space, failed units, restic locks.\ndf -h /mnt/tank\nsystemctl --failed --no-legend\necho '--- restic locks'\nrestic -r \"$RESTIC_REPO\" list locks --no-lock\n",
        "reports/morning-report.html": (
            "<!doctype html><meta charset=utf-8><title>Morning report</title>"
            "<style>body{font:16px system-ui;margin:40px auto;max-width:760px;color:#1f2933}h1{font-size:26px}"
            "table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #e4e7eb;padding:8px;text-align:left}"
            ".ok{color:#1f9d55}.warn{color:#b7791f}</style>"
            "<h1>Morning report</h1><p>Generated by Atlas for the home lab.</p><table>"
            "<tr><th>Check</th><th>Result</th></tr><tr><td>NAS pool <code>tank</code></td><td class=ok>65% used, 7.6 TB free</td></tr>"
            "<tr><td>Nightly backup</td><td class=ok>Finished 09:14, 412 MiB added</td></tr>"
            "<tr><td>Restic locks</td><td class=ok>None</td></tr><tr><td>Failed services</td><td class=ok>None</td></tr>"
            "<tr><td>Updates</td><td class=warn>Jellyfin 10.11.2 available</td></tr></table>"
        ),
    }
    for relative, text in files.items():
        path = workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        path.chmod(0o666)
    for folder in [workspace, *{(workspace / relative).parent for relative in files}, workspace / "projects"]:
        folder.chmod(0o777)

print(json.dumps({
    "computer": computer, "computer_bots": [atlas["id"]] if computer else [],
    "owner_token": owner_token, "sam_token": sam_token,
    "bots": {"atlas": atlas["id"], "iris": iris["id"], "quill": quill["id"], "cubie": cubie["id"]},
    "conversations": {"backup": backup_conv["id"], "disk": disk["id"], "drives": drives["id"], "garden": garden["id"], "weekly": weekly["id"]},
    "projects": {"backup": backup["id"]},
}))

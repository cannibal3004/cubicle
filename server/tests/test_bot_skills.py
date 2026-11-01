"""A bot's own skills with files: written from a folder, installed into one bot, copied from the library,
winning over a library skill of the same name, and moved to the library files and all."""

import asyncio
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from app.routers import bot_skills as bot_skills_router
from app.routers import bots as bots_router
from app.routers import skills as skills_router
from app.services import procedures, skills
from app.services import storage_service as storage_module
from app.services.llm_tools import build_invocation
from app.services.storage_service import StorageService

REPORT_SKILL = "---\nname: weekly-report\ndescription: Write the Monday status report.\n---\nRun scripts/gather.sh, then fill templates/report.md.\n"


def zipped(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path, text in files.items():
            archive.writestr(path, text)
    return buffer.getvalue()


def as_dict(bot):
    return bot if isinstance(bot, dict) else bot.model_dump()


class BotSkillTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.storage = StorageService(Path(self.temp.name))
        self.patch = mock.patch.object(storage_module, "storage_service", self.storage)
        self.patch.start()
        bots = self.storage.get_bots()
        self.bot = bots[0]["id"]
        bots.append({**bots[0], "id": "bot-other", "name": "Other", "procedures": []})
        self.storage.save_bots(bots)
        self.computer = Path(self.temp.name) / "computer"
        self.computer.mkdir()
        self.root = mock.patch("app.services.file_share.source_root", lambda source, bot_id: self.computer)
        self.root.start()

    def tearDown(self):
        self.root.stop()
        self.patch.stop()
        try:
            self.temp.cleanup()
        except PermissionError:
            pass

    def record(self, bot_id=None):
        return next(b for b in self.storage.get_bots() if b["id"] == (bot_id or self.bot))

    def read(self, name, bot_id=None, **extra):
        return asyncio.run(skills.execute_read(build_invocation("skill_read", {"name": name, **extra}, bot_id or self.bot)))

    def test_a_bot_keeps_a_skill_with_the_files_of_a_folder(self):
        work = self.computer / "work" / "report"
        (work / "scripts").mkdir(parents=True)
        (work / "scripts" / "gather.sh").write_bytes(b"#!/bin/sh\necho gathered\n")
        (work / "SKILL.md").write_bytes(REPORT_SKILL.encode())
        call = build_invocation("skill_save", {"name": "weekly-report", "folder": "/workspace/work/report"}, self.bot)
        self.assertIn("with the files of /workspace/work/report", call.preview)
        saved = asyncio.run(skills.execute_save(call))
        self.assertEqual(saved["files"], ["scripts/gather.sh"])
        own = procedures.list_procedures(self.bot)[0]
        self.assertEqual((own["steps"], own["description"]), ("Run scripts/gather.sh, then fill templates/report.md.", "Write the Monday status report."))
        self.assertIn("[1 files]", skills.prompt_block(self.record()))
        self.assertNotIn("weekly-report", skills.prompt_block(self.record("bot-other")))  # this bot's alone
        # Read: the files land in its computer, SKILL.md and all.
        result = self.read("weekly-report")
        self.assertEqual((result["yours"], result["folder"], result["files"]), (True, "/workspace/skills/weekly-report", ["scripts/gather.sh"]))
        self.assertEqual((self.computer / "skills/weekly-report/scripts/gather.sh").read_text(), "#!/bin/sh\necho gathered\n")
        self.assertIn("name: weekly-report", (self.computer / "skills/weekly-report/SKILL.md").read_text())
        self.assertEqual(self.read("weekly-report", file="scripts/gather.sh")["text"], "#!/bin/sh\necho gathered\n")
        # Saved again without a folder: new instructions, the files stay.
        asyncio.run(skills.execute_save(build_invocation("skill_save", {"name": "weekly-report", "instructions": "1. Run scripts/gather.sh."}, self.bot)))
        own = procedures.list_procedures(self.bot)[0]
        self.assertEqual((own["steps"], [f["path"] for f in own["files"]]), ("1. Run scripts/gather.sh.", ["scripts/gather.sh"]))
        # The settings page sends the list back: the files are the server's, not the page's.
        sent = [{**own, "files": [], "description": "Edited."}]
        updated = as_dict(asyncio.run(bots_router.update_bot(self.bot, bots_router.BotUpdate(procedures=sent))))
        self.assertEqual(updated["procedures"][0]["files"], own["files"])
        self.assertNotEqual(updated["procedures"][0]["version"], own["version"])  # the computer's copy is refreshed
        # Removed there: its files go too.
        folder = procedures.folder(self.bot, own["id"])
        self.assertTrue(folder.is_dir())
        asyncio.run(bots_router.update_bot(self.bot, bots_router.BotUpdate(procedures=[])))
        self.assertFalse(folder.exists())

    def test_installed_into_one_bot_or_copied_from_the_library_and_the_bots_own_wins(self):
        data = zipped({"weekly-report/SKILL.md": REPORT_SKILL, "weekly-report/scripts/gather.sh": "echo gathered", "weekly-report/templates/report.md": "# Report"})
        installed = skills.install_upload("report.zip", data, skills.BotSkillStore(self.bot))
        self.assertEqual([p["title"] for p in installed], ["weekly-report"])
        self.assertEqual(sorted(f["path"] for f in installed[0]["files"]), ["scripts/gather.sh", "templates/report.md"])
        self.assertEqual(installed[0]["source"]["kind"], "upload")
        self.assertEqual(procedures.list_procedures("bot-other"), [])
        # A library skill of the same name is left out for this bot only.
        library = skills.user_skill_store.install({"SKILL.md": REPORT_SKILL.replace("Run scripts", "Library: run scripts").encode()}, source={"kind": "upload"})
        self.assertNotIn(library["id"], [r["id"] for r in skills.bot_library(self.record())])
        self.assertIn(library["id"], [r["id"] for r in skills.bot_library(self.record("bot-other"))])
        self.assertTrue(self.read("weekly-report")["yours"])
        self.assertTrue(self.read("weekly-report", "bot-other")["instructions"].startswith("Library:"))
        # Copied from the library into the other bot: its own from then on, files and all.
        pdf = skills.user_skill_store.install({"SKILL.md": b"---\nname: pdf\ndescription: Fill PDF forms.\n---\nRun scripts/fill.py.", "scripts/fill.py": b"print(1)"},
                                              source={"kind": "upload"})
        copied = asyncio.run(bot_skills_router.from_library("bot-other", bot_skills_router.FromLibraryInput(skill_id=pdf["id"])))
        own = as_dict(copied["bot"])["procedures"]
        self.assertEqual(([p["title"] for p in own], own[0]["files"], own[0]["source"]["kind"]), (["pdf"], [{"path": "scripts/fill.py", "size": 8}], "library"))
        self.assertNotIn(pdf["id"], [r["id"] for r in skills.bot_library(self.record("bot-other"))])
        self.assertEqual(asyncio.run(bot_skills_router.read_file("bot-other", own[0]["id"], "scripts/fill.py"))["text"], "print(1)")
        # Too long for a bot's own: the library is the place.
        with self.assertRaisesRegex(skills.SkillError, "library"):
            skills.BotSkillStore(self.bot).install({"SKILL.md": ("---\nname: huge\n---\n" + "x" * 70_000).encode()}, source={"kind": "upload"})

    def test_moved_to_the_library_with_its_files_and_gone_with_the_bot(self):
        installed = skills.install_upload("r.zip", zipped({"SKILL.md": REPORT_SKILL, "scripts/gather.sh": "echo gathered"}), skills.BotSkillStore(self.bot))[0]
        moved = asyncio.run(skills_router.move_from_bot(skills_router.FromBotInput(bot_id=self.bot, skill_id=installed["id"])))
        self.assertEqual([f["path"] for f in moved["skill"]["files"]], ["scripts/gather.sh"])
        self.assertEqual(skills.user_skill_store.files_of(moved["skill"])["scripts/gather.sh"], b"echo gathered")
        self.assertFalse(procedures.folder(self.bot, installed["id"]).exists())
        again = skills.install_upload("r.zip", zipped({"SKILL.md": REPORT_SKILL.replace("weekly-report", "other-report"), "a.txt": "a"}),
                                      skills.BotSkillStore("bot-other"))[0]
        self.assertTrue(procedures.folder("bot-other", again["id"]).is_dir())
        asyncio.run(bots_router.delete_bot("bot-other"))
        self.assertFalse(procedures.bot_folder("bot-other").exists())


if __name__ == "__main__":
    unittest.main()

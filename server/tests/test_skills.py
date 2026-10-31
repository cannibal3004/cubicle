"""Skills: SKILL.md folders in a library (system and per account), and the bot's own (former procedures)."""

import asyncio
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi import HTTPException

from app.routers import bots as bots_router
from app.routers import skills as skills_router
from app.services import procedures, skills
from app.services import storage_service as storage_module
from app.services.llm_tools import build_invocation
from app.services.storage_service import StorageService

PDF_SKILL = """---
name: pdf
description: >
  Fill and merge PDF forms. Use when the user gives you a PDF form
  or asks to combine PDFs.
license: MIT
---

# PDF

To fill a form run `python scripts/fill.py form.pdf values.json`; field names are in reference/fields.md.
"""


def zipped(files, *, links=()):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path, text in files.items():
            archive.writestr(path, text)
        for path in links:
            info = zipfile.ZipInfo(path)
            info.external_attr = (0o120777 << 16)
            archive.writestr(info, "/etc/passwd")
    return buffer.getvalue()


class SkillFilesTests(unittest.TestCase):
    def test_skill_md_front_matter_and_instructions(self):
        meta, body = skills.parse_skill_md(PDF_SKILL)
        self.assertEqual(meta["name"], "pdf")
        self.assertTrue(meta["description"].startswith("Fill and merge PDF forms. Use when"))
        self.assertTrue(body.startswith("# PDF"))
        self.assertEqual(skills.parse_skill_md("Just instructions."), ({}, "Just instructions."))
        with self.assertRaisesRegex(skills.SkillError, "not valid YAML"):
            skills.parse_skill_md("---\nname: [unclosed\n---\nbody")
        again = skills.parse_skill_md(skills.compose_skill_md({"name": "x", "description": "y: z"}, "Do it."))
        self.assertEqual(again, ({"name": "x", "description": "y: z"}, "Do it.\n"))

    def test_archives_are_read_safely_and_split_into_skills(self):
        data = zipped(
            {
                "repo-main/README.md": "a repo",
                "repo-main/skills/pdf/SKILL.md": PDF_SKILL,
                "repo-main/skills/pdf/scripts/fill.py": "print('fill')",
                "repo-main/skills/pdf/examples/inner/SKILL.md": "---\nname: inner\ndescription: d\n---\nx",
                "repo-main/skills/docx/SKILL.md": "---\nname: docx\ndescription: Word files\n---\nUse python-docx.",
                "repo-main/../escape.txt": "nope",
                "/abs.txt": "nope",
            },
            links=["repo-main/skills/pdf/link"],
        )
        files = skills.files_from_zip(data, strip_top=True)
        self.assertNotIn("../escape.txt", files)
        self.assertFalse(any("link" in path or "abs" in path or "escape" in path for path in files))
        found = dict(skills.split_skills(files))
        self.assertEqual(sorted(found), ["skills/docx", "skills/pdf"])  # the inner one belongs to pdf
        self.assertIn("examples/inner/SKILL.md", found["skills/pdf"])
        only_pdf = skills.files_from_zip(data, strip_top=True, within="skills/pdf")
        self.assertEqual(sorted(only_pdf), ["SKILL.md", "examples/inner/SKILL.md", "scripts/fill.py"])
        with self.assertRaisesRegex(skills.SkillError, "No SKILL.md"):
            skills.split_skills({"README.md": b"x"})
        with self.assertRaisesRegex(skills.SkillError, "not a zip"):
            skills.files_from_zip(b"not a zip")

    def test_links_name_what_to_fetch(self):
        repo = skills.source_for("https://github.com/anthropics/skills")
        self.assertEqual((repo["archive"], repo["within"]), ("https://github.com/anthropics/skills/archive/HEAD.zip", ""))
        folder = skills.source_for("https://github.com/anthropics/skills/tree/main/skills/pdf")
        self.assertEqual((folder["archive"], folder["within"], folder["name"]), ("https://github.com/anthropics/skills/archive/main.zip", "skills/pdf", "pdf"))
        blob = skills.source_for("https://github.com/anthropics/skills/blob/main/skills/pdf/SKILL.md")
        self.assertEqual(blob["within"], "skills/pdf")
        self.assertEqual(skills.source_for("https://example.com/my-skill.zip")["kind"], "zip")
        self.assertEqual(skills.source_for("https://raw.githubusercontent.com/a/b/main/x/SKILL.md")["kind"], "file")
        for bad in ("ftp://x/y.zip", "https://example.com/page", "https://github.com/a/b/blob/main/README.md"):
            with self.assertRaises(skills.SkillError):
                skills.source_for(bad)


class SkillLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.storage = StorageService(Path(self.temp.name))
        self.patch = mock.patch.object(storage_module, "storage_service", self.storage)
        self.patch.start()
        self.bot = self.storage.get_bots()[0]

    def tearDown(self):
        self.patch.stop()
        try:
            self.temp.cleanup()
        except PermissionError:
            pass

    def test_install_replace_edit_and_remove(self):
        store = skills.user_skill_store
        files = {"SKILL.md": PDF_SKILL.encode(), "scripts/fill.py": b"print('fill')", "reference/fields.md": b"name, date"}
        record = store.install(files, source={"kind": "upload"})
        self.assertEqual((record["name"], record["scope"]), ("pdf", "user"))
        self.assertEqual([f["path"] for f in record["files"]], ["reference/fields.md", "scripts/fill.py"])
        self.assertTrue((store.folder(record) / "scripts/fill.py").is_file())
        # Installed again (a newer version): same id, files replaced whole.
        again = store.install({"SKILL.md": PDF_SKILL.encode(), "scripts/fill2.py": b"v2"}, source={"kind": "upload"})
        self.assertEqual(again["id"], record["id"])
        self.assertFalse((store.folder(record) / "scripts/fill.py").exists())
        self.assertEqual(len(store.list()), 1)
        # Edited here: the rest of the front matter and the other files stay.
        edited = store.update(record["id"], description="Fill PDF forms.", instructions="Run scripts/fill2.py.")
        self.assertEqual(edited["description"], "Fill PDF forms.")
        meta, body = skills.parse_skill_md(store.skill_md(edited))
        self.assertEqual((meta["license"], body.strip()), ("MIT", "Run scripts/fill2.py."))
        self.assertTrue((store.folder(record) / "scripts/fill2.py").is_file())
        self.assertEqual(store.read_file(edited, "scripts/fill2.py"), "v2")
        with self.assertRaisesRegex(skills.SkillError, "has no file"):
            store.read_file(edited, "../../secret")
        written = store.create("Weekly report", "Write the Monday status report.", "1. Gather.\n2. Write.")
        self.assertEqual(written["files"], [])
        with self.assertRaisesRegex(skills.SkillError, "already exists"):
            store.create("weekly report", "d", "x")
        with self.assertRaisesRegex(skills.SkillError, "needs a description"):
            store.create("Other", "", "x")
        store.remove(record["id"])
        self.assertFalse(store.folder(record).exists())
        self.assertEqual([s["name"] for s in skills.library()], ["Weekly report"])
        # System skills are kept apart, and come first.
        skills.system_skill_store.create("House style", "How we write.", "Plain words.")
        self.assertEqual([(s["name"], s["scope"]) for s in skills.library()], [("House style", "system"), ("Weekly report", "user")])

    def test_installing_from_a_github_folder(self):
        archive = zipped({"skills-main/skills/pdf/SKILL.md": PDF_SKILL, "skills-main/skills/pdf/scripts/fill.py": "x", "skills-main/skills/docx/SKILL.md": "---\nname: docx\ndescription: Word\n---\nx"})

        async def fake_download(url):
            self.assertEqual(url, "https://github.com/anthropics/skills/archive/main.zip")
            return archive

        with mock.patch.object(skills, "_download", fake_download):
            one = asyncio.run(skills.install_from_url("https://github.com/anthropics/skills/tree/main/skills/pdf", skills.user_skill_store))
            self.assertEqual([r["name"] for r in one], ["pdf"])
            self.assertEqual(one[0]["source"]["url"], "https://github.com/anthropics/skills/tree/main/skills/pdf")
            both = asyncio.run(skills.install_from_url("https://github.com/anthropics/skills/tree/main/skills", skills.user_skill_store))
        self.assertEqual(sorted(r["name"] for r in both), ["docx", "pdf"])
        self.assertEqual(len(skills.library()), 2)  # pdf replaced, not doubled

    def test_a_bot_lists_reads_and_gets_the_files_in_its_computer(self):
        record = skills.user_skill_store.install({"SKILL.md": PDF_SKILL.encode(), "scripts/fill.py": b"print('fill')"}, source={"kind": "upload"})
        other = skills.user_skill_store.create("Weekly report", "Write the Monday status report.", "1. Gather.")
        procedures.save_procedure(self.bot["id"], "File a PTO request", "1. Open Workday.", description="Time off")
        bot = next(b for b in self.storage.get_bots() if b["id"] == self.bot["id"])
        block = skills.prompt_block(bot)
        self.assertIn("  - pdf: Fill and merge PDF forms.", block)
        self.assertIn("  - Weekly report: Write the Monday status report.", block)
        self.assertIn('"File a PTO request" (Time off)', block)
        # Switched off for this bot: not listed, not readable.
        self.assertNotIn("Weekly report", skills.prompt_block({**bot, "skill_settings": {other["id"]: False}}))

        computer = Path(self.temp.name) / "computer"
        computer.mkdir()
        with mock.patch("app.services.file_share.source_root", lambda source, bot_id: computer):
            result = asyncio.run(skills.execute_read(build_invocation("skill_read", {"name": "PDF"}, bot["id"])))
            self.assertTrue(result["instructions"].startswith("# PDF"))
            self.assertEqual((result["folder"], result["files"]), ("/workspace/skills/pdf", ["scripts/fill.py"]))
            self.assertEqual((computer / "skills/pdf/scripts/fill.py").read_text(), "print('fill')")
            self.assertTrue((computer / "skills/pdf/SKILL.md").is_file())
            # Up to date: not copied again (the bot may have changed its copy).
            (computer / "skills/pdf/scripts/fill.py").write_text("changed")
            asyncio.run(skills.execute_read(build_invocation("skill_read", {"name": "pdf"}, bot["id"])))
            self.assertEqual((computer / "skills/pdf/scripts/fill.py").read_text(), "changed")
            # A newer version is.
            skills.user_skill_store.update(record["id"], description="Newer.")
            asyncio.run(skills.execute_read(build_invocation("skill_read", {"name": "pdf"}, bot["id"])))
            self.assertEqual((computer / "skills/pdf/scripts/fill.py").read_text(), "print('fill')")
        one_file = asyncio.run(skills.execute_read(build_invocation("skill_read", {"name": "pdf", "file": "scripts/fill.py"}, bot["id"])))
        self.assertEqual(one_file["text"], "print('fill')")
        own = asyncio.run(skills.execute_read(build_invocation("skill_read", {"name": "pto"}, bot["id"])))
        self.assertEqual((own["instructions"], own["yours"]), ("1. Open Workday.", True))
        with self.assertRaisesRegex(skills.SkillError, "Your skills"):
            asyncio.run(skills.execute_read(build_invocation("skill_read", {"name": "nothing"}, bot["id"])))
        # A skill switched off for the bot cannot be read either.
        bots = self.storage.get_bots()
        next(b for b in bots if b["id"] == bot["id"])["skill_settings"] = {other["id"]: False}
        self.storage.save_bots(bots)
        with self.assertRaisesRegex(skills.SkillError, "No skill called"):
            asyncio.run(skills.execute_read(build_invocation("skill_read", {"name": "weekly"}, bot["id"])))

    def test_the_bot_editor_keeps_only_what_is_switched_off(self):
        updated = asyncio.run(bots_router.update_bot(self.bot["id"], bots_router.BotUpdate(skill_settings={"skill-1a2b3c4d": False, "skill-00000000": True, "bad id": False})))
        settings = updated["skill_settings"] if isinstance(updated, dict) else updated.skill_settings
        self.assertEqual(settings, {"skill-1a2b3c4d": False})

    def test_only_an_admin_changes_system_skills(self):
        member = SimpleNamespace(state=SimpleNamespace(user={"id": "u2", "username": "bob", "role": "member"}))
        admin = SimpleNamespace(state=SimpleNamespace(user={"id": "local-user", "username": "jamie", "role": "owner"}))
        body = skills_router.SkillInput(name="House style", description="How we write.", instructions="Plain words.", scope="system")
        with self.assertRaises(HTTPException) as refused:
            asyncio.run(skills_router.create(body, member))
        self.assertEqual(refused.exception.status_code, 403)
        created = asyncio.run(skills_router.create(body, admin))["skill"]
        with self.assertRaises(HTTPException):
            asyncio.run(skills_router.remove(created["id"], member))
        mine = asyncio.run(skills_router.create(skills_router.SkillInput(name="Mine", description="d", instructions="x"), member))["skill"]
        self.assertEqual(mine["scope"], "user")
        detail = asyncio.run(skills_router.read(created["id"]))
        self.assertEqual(detail["instructions"], "Plain words.")


if __name__ == "__main__":
    unittest.main()

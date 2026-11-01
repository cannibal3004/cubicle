# Skills

A skill is a set of instructions for a kind of task (filling in PDF forms, writing the weekly report,
renewing a certificate), with any scripts, templates and reference files the instructions use. A bot
sees the name and description of every skill it has and reads one in full when a task fits it, so a
large library costs little until a skill is needed.

Skills use the open **Agent Skills** format that other assistants use too: a folder with a `SKILL.md`
whose top gives a `name` and a `description`, followed by the instructions in Markdown:

```markdown
---
name: weekly-report
description: Write the Monday status report. Use when asked for the weekly report or a status update.
---

1. Read the work notes of every active project.
2. ...
```

Other files in the folder (`scripts/`, `reference/`, templates) are read or run only when the
instructions call for them.

## The library

App Settings → **Skills** lists your skills, and the system skills an admin added for every account.

![App Settings: Skills](images/app-settings-skills.png)

- **Install** from a link:
  - a GitHub folder holding a skill, such as
    `https://github.com/anthropics/skills/tree/main/skills/pdf`
  - a whole repository, which installs every skill in it
  - a link to a `.zip`
  - a link to a single `SKILL.md`
- **Upload** a `.zip` of one or more skill folders, or a lone `SKILL.md`.
- **Write one** here: a name, a description and the instructions.
- The pencil edits a skill's name, description and instructions; its other files stay as they are.
  Installing a skill with the same name again replaces it with the new version.

Write the **description** for the bot: what the skill does and when to use it. The bot decides from it
alone whether to read the skill.

Only install skills you trust. A bot follows a skill's instructions, and its scripts run in the bot's
computer (with the usual [approvals](approvals.md) for commands).

## A bot's skills

Every bot has every library skill unless you switch it off: bot settings → **Notes & skills** →
**Skills**.

When a bot reads a skill, its files are copied into the bot's computer under `/workspace/skills/<name>`,
and paths in the instructions are relative to that folder, so a skill's scripts run there. The copy is
replaced when the skill changes. A bot without a computer can still read a skill and its text files.

## A bot's own skills

A bot also keeps **skills of its own**, listed under **This bot's own skills** in its settings. They
belong to that bot alone, and they go with it: a teammate you [share](sharing.md) the bot with has
them too, and no other bot does. Like library skills, they can carry files (scripts, references,
templates) besides the instructions. A bot's own skill comes from:

- **The bot itself**, with `skill_save`, most often after you
  [showed it how](computer.md#showing-the-bot-how) to do something in its computer's browser. Ask a
  bot to "keep that as a skill" after it worked something out, and it writes one for next time; to
  keep scripts with it, it names a folder in its computer and the files there become part of the
  skill. It corrects one when it learns that a step has changed. (These used to be called procedures.)
- **Installing it for that bot**: paste a link (a GitHub folder or repository, a `.zip`, or a
  `SKILL.md`) or upload a zip at the bottom of **This bot's own skills**.
- **Copying a library skill**: **Copy to bot**, next to a skill under **Skills**, makes a copy that is
  the bot's to change for itself alone.

When a bot's own skill has the same name as a library skill, the bot uses its own, and the library one
is marked as replaced in that bot's list. Open a skill in the list to read its instructions and files,
and × removes it (when you save).

## From a bot to every bot

A skill of one bot's can serve all of them:

- **To library**, next to a skill under **This bot's own skills**, moves it, files and all, into your
  library: every bot gets it, and it leaves that bot's own list.
- A bot can add a skill to your library itself with `skill_publish` (it asks first): ask it to "make
  that a skill for all the bots". It names a folder in its computer, and the scripts, references and
  templates in it become part of the skill. The library shows who wrote it.

# Projects

A project is a piece of work that spans conversations: a backup overhaul, a migration, a report you
come back to every week. It keeps three things together:

- **Work notes**: where the work stands now (status, decisions, next steps). Every conversation in the
  project sees the same notes, and the bot keeps them up to date.
- **A log**: what happened along the way, newest first. The bot adds to it as it works, and searches it
  when a message touches on something from the past.
- **A folder** in the bot's computer, `/workspace/projects/<name>`, for the project's files.

## The project page

![A project's page](images/project-page.png)

Click a project's name in the sidebar to open its page (the arrow beside it folds its conversations
away). The page shows the status line (the first line of the work notes), the full work notes (with
**Edit**), the log (each entry links to the conversation it came from), the conversations in the
project, and the folder with a button to open it in the [file viewer](computer.md#files).

From the header you can rename the project (click its name), start a new conversation in it, open its
files, archive it, or delete it. Deleting removes the work notes and the log; the conversations stay,
outside any project, and so do the files in its folder.

A project page has its own address (`/p/<id>`) like a conversation does.

## Making a project and moving conversations into it

- **Project** under a bot's **New conversation** button makes one: give it a name.
- Hover a conversation in the sidebar and click the folder icon to move it into a project (or out of
  one). The **+** on a project starts a new conversation inside it.
- Or just ask the bot: "make a project for the backup overhaul and move this conversation into it." It
  has tools for that (`project_create`, `project_join`), and it is told about the folder.

When a conversation joins a project, the bot is shown the project's work notes and recent log there and
then. Its own work notes (from before it joined) are kept and come back if it leaves the project.

## When the work notes change

If the bot changes a project's work notes in one conversation, the project's other conversations are
told the notes changed, so the bot there reads them again before relying on them. Your own edits on the
project page or in the to-do panel are shown to the bot in each of the project's conversations.

See [Notes, work notes and to-dos](notes-and-todos.md) for how a project's notes relate to the bot's own
notes.

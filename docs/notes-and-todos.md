# Notes, work notes and to-dos

A bot keeps three kinds of written memory, each for a different job.

| | What it is for | Where it lives | Where you see it |
| --- | --- | --- | --- |
| **Notes** | Durable facts about the bot's job: the systems it looks after, how to reach them, house rules, lessons learned | The bot, shared by all its conversations | [Bot settings](bots.md) → Notes & procedures |
| **Work notes** | Where one piece of work stands: status, decisions, next steps | The conversation, or its [project](projects.md) | The to-do panel; the project page |
| **To-do list** | What is left to do on the job at hand | The conversation | The to-do panel |

![The to-do panel with work notes](images/todo-panel.png)

## Notes

The bot reads its notes at the start of each conversation and writes them with `memory_write` (a full
rewrite) and `memory_edit` (one passage). You can edit them on the bot's settings page; your edit is
shown to the bot in its conversations, so it knows what changed. Notes are limited to 16,000 characters
by default, which keeps them to what matters.

If a bot's notes have grown into a mix of facts and project status, **Have the bot sort these into notes
and projects…** under the notes starts a conversation in which the bot moves each piece of work into a
project of its own and keeps only the durable facts in its notes.

## Work notes

Work notes say where the work stands now. In a conversation that is in no project they belong to the
conversation; in a project, every conversation in it shares the project's work notes. The bot keeps
them with `work_notes_write` and `work_notes_edit`. You can edit them in the to-do panel or on the
project page.

What happened along the way does not go in the work notes but in the project's log, which the bot
writes with `project_log` and searches with `project_log_read`.

## To-do list

The to-do list is the bot's plan for the job at hand. It adds steps with `todo_add` and moves each one
through open, doing and done with `todo_update`. You can tick items, reword them, add and remove them in
the panel; your changes are shown to the bot.

If a reply has to stop before the list is done (the context is full, or the bot stopped making
progress), the bot is asked to name what is left, so the next reply picks it up.

## Why none of this goes in the system prompt

Notes, work notes and to-dos travel with the conversation, not in the bot's instructions. That keeps the
start of the prompt the same from one message to the next, so the model server can reuse its cache and
replies stay fast. After a compaction, the summary carries the current notes, work notes and list.

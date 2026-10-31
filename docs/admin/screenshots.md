# Updating these screenshots

The pictures in these pages are taken by a script, from a throwaway copy of Cubicle filled with made-up
data, so they can be re-taken in one command after the UI changes.

```bash
pip install playwright
playwright install chromium        # or pass --channel msedge / chrome to use an installed browser
python tools/docs/screenshots.py
```

It:

1. starts a separate Docker Compose project, `cubicle-docs`, on `http://localhost:38080`, with volumes
   of its own (nothing touches a Cubicle you already run);
2. seeds it with made-up people, bots, conversations, projects, to-dos, schedules and secrets
   (`tools/docs/seed_demo.py`);
3. photographs it into `docs/images/` (`tools/docs/shoot.py`, one function per picture);
4. removes it again, volumes and all.

No model server is needed: the stack talks to `tools/docs/mock_llm.py`, a scripted stand-in that lists
one model and answers a few set prompts the same way every time, which is how the approval card and the
thinking-as-it-streams pictures are taken.

## Options

- `--only overview,project_page`: take just those pictures (the function names in `shoot.py`).
- `--keep`: leave the stack running afterwards to look around in (sign in as `alex` /
  `demo-password-1234`); remove it with `docker compose -p cubicle-docs down -v`.
- `--computer`: include the bot's real computer, for the desktop and file viewer pictures. This needs
  the runtime image (`docker build -t cubicle-computer:2.14.0 ./runtime`, done for you if it is
  missing); the bot that used a computer is deleted at the end, which removes its container.
- `--no-build`: reuse the images from the last run.

## Adding a picture

Add a function to `shoot.py` decorated with `@shot` (or `@computer_shot` when it needs the real
computer). It gets a signed-in Playwright page; open what you want with `open_page(page, path)` and save
it with `save(page, "name")`, or `save(page, "name", locator)` to crop to an element. If it needs data
the seed doesn't have, add it to `seed_demo.py`: everything there is invented, so keep it that way.

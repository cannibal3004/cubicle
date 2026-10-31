"""Photograph the docs stack (seeded by seed_demo.py) into docs/images/.

usage: python tools/docs/shoot.py <seed json file> [--base URL] [--channel msedge] [--only name,name]

Each shot is a function below: it opens a page, gets it into the state the picture
shows, and saves one PNG. Pictures of a part of the window crop to that part.
"""

import argparse
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "docs" / "images"
VIEWPORT = {"width": 1440, "height": 900}

parser = argparse.ArgumentParser()
parser.add_argument("seed")
parser.add_argument("--base", default="http://localhost:38080")
parser.add_argument("--channel", default=None, help="a browser Playwright can drive, e.g. msedge or chrome; default its own chromium")
parser.add_argument("--only", default="", help="comma-separated shot names")
args = parser.parse_args()
seed = json.loads(Path(args.seed).read_text(encoding="utf-8"))
BASE = args.base.rstrip("/")
C, P, B = seed["conversations"], seed["projects"], seed["bots"]
SHOTS = []


def shot(func):
    SHOTS.append(func)
    return func


def save(page, name, target=None, pad=12):
    """The whole window, or `target` (a locator) with a little room around it."""
    path = OUT / f"{name}.png"
    if target is None:
        page.screenshot(path=str(path))
    else:
        box = target.bounding_box()
        clip = {"x": max(0, box["x"] - pad), "y": max(0, box["y"] - pad)}
        size = page.viewport_size
        clip["width"] = min(size["width"] - clip["x"], box["width"] + 2 * pad)
        clip["height"] = min(size["height"] - clip["y"], box["height"] + 2 * pad)
        page.screenshot(path=str(path), clip=clip)
    print("  saved", path.relative_to(ROOT))


def open_page(page, path, wait=1800, height=None):
    """`height`: a taller window, for a picture of something longer than the usual one."""
    page.set_viewport_size({"width": VIEWPORT["width"], "height": height or VIEWPORT["height"]})
    page.goto(f"{BASE}{path}")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(wait)


# ---- the chat --------------------------------------------------------------------------------

@shot
def overview(page):
    open_page(page, f"/c/{C['backup']}")
    # The question at the top, the reply below it.
    page.get_by_text("The nightly backup to the NAS failed again").evaluate(
        "el => { el.scrollIntoView({block: 'start'}); const list = el.closest('.overflow-y-auto'); if (list) list.scrollTop -= 64; }"
    )
    page.wait_for_timeout(400)
    save(page, "overview")


@shot
def chat_reply(page):
    open_page(page, f"/c/{C['backup']}", height=1700)
    save(page, "chat-reply", page.locator(".dark-bubble-bot").first)


@shot
def tool_step_details(page):
    open_page(page, f"/c/{C['backup']}")
    step = page.locator("button[aria-expanded]", has_text="computer_terminal_execute").first
    step.click()
    page.wait_for_timeout(500)
    save(page, "tool-step-details", step.locator("xpath=.."))


@shot
def tool_step_hover(page):
    open_page(page, f"/c/{C['backup']}")
    step = page.locator("button[aria-expanded]", has_text="computer_terminal_execute").nth(1)
    step.hover()
    page.wait_for_timeout(700)
    card = page.locator("div.fixed.z-50").last
    box_step, box_card = step.bounding_box(), card.bounding_box()
    top = min(box_step["y"], box_card["y"]) - 12
    bottom = max(box_step["y"] + box_step["height"], box_card["y"] + box_card["height"]) + 12
    page.screenshot(path=str(OUT / "tool-step-hover.png"), clip={"x": box_step["x"] - 12, "y": top, "width": box_step["width"] + 24, "height": bottom - top})
    print("  saved docs/images/tool-step-hover.png")


@shot
def thinking_block(page):
    open_page(page, f"/c/{C['backup']}", height=1700)
    page.get_by_role("button", name="Thinking").nth(1).click()
    page.wait_for_timeout(400)
    save(page, "thinking-block", page.locator(".dark-bubble-bot").first)


@shot
def thinking_chip(page):
    open_page(page, f"/c/{C['backup']}")
    chip = page.get_by_title("How hard the model thinks in this conversation (/think)")
    chip.click()
    page.wait_for_timeout(400)
    menu = chip.locator("xpath=..")
    box = menu.bounding_box()
    page.screenshot(path=str(OUT / "thinking-menu.png"), clip={"x": box["x"] - 260, "y": box["y"] - 10, "width": 420, "height": 330})
    print("  saved docs/images/thinking-menu.png")


@shot
def todo_panel(page):
    open_page(page, f"/c/{C['backup']}")
    panel = page.locator("aside[aria-label='To-do list and work notes']")
    box = panel.bounding_box()
    content = panel.evaluate("el => el.firstElementChild.lastElementChild.getBoundingClientRect().bottom - el.getBoundingClientRect().top")
    page.screenshot(path=str(OUT / "todo-panel.png"), clip={"x": box["x"], "y": box["y"], "width": box["width"], "height": min(box["height"], content + 8)})
    print("  saved docs/images/todo-panel.png")


@shot
def suggestions(page):
    open_page(page, f"/c/{B['quill']}")
    save(page, "suggestions")


@shot
def slash_commands(page):
    open_page(page, f"/c/{C['disk']}")
    page.locator("form textarea").fill("/")
    page.wait_for_timeout(500)
    form = page.locator("form:has(textarea)").first
    box = form.bounding_box()
    page.screenshot(path=str(OUT / "slash-commands.png"), clip={"x": box["x"] - 12, "y": max(0, box["y"] - 420), "width": box["width"] + 24, "height": box["height"] + 432})
    print("  saved docs/images/slash-commands.png")


@shot
def file_handover(page):
    open_page(page, f"/c/{C['drives']}", height=1400)
    save(page, "file-handover", page.locator(".dark-bubble-bot").last)


@shot
def cubie(page):
    open_page(page, f"/c/{C['garden']}")
    save(page, "cubie")


@shot
def scheduled_run(page):
    open_page(page, f"/c/{C['weekly']}")
    save(page, "scheduled-run")


# ---- live moments, answered by the scripted model (mock_llm.py) ------------------------------

def throwaway(page, bot_id, title):
    return page.request.post(f"{BASE}/api/v1/bots/{bot_id}/conversations", data={"title": title}).json()["id"]


def finish_and_delete(page, thread_id):
    page.get_by_title("Send (Enter)").wait_for(timeout=60000)
    page.wait_for_timeout(800)
    for _ in range(10):
        if page.request.delete(f"{BASE}/api/v1/conversations/{thread_id}").status != 409:
            return
        page.wait_for_timeout(1000)


@shot
def approval_card(page):
    thread = throwaway(page, B["iris"], "Tidy the shared folder")
    try:
        open_page(page, f"/c/{thread}")
        page.locator("form textarea").fill("Please delete the old drive comparison from 2025 in the shared folder.")
        page.get_by_title("Send (Enter)").click()
        deny = page.get_by_role("button", name="Deny")
        deny.wait_for(timeout=30000)
        page.wait_for_timeout(600)
        save(page, "approval-card")
        deny.click()
    finally:
        finish_and_delete(page, thread)


@shot
def live_thinking(page):
    thread = throwaway(page, B["atlas"], "How long until the NAS is full?")
    try:
        open_page(page, f"/c/{thread}")
        page.locator("form textarea").fill("How long until the NAS pool is full? Think it over before you answer.")
        page.get_by_title("Send (Enter)").click()
        page.get_by_title("Stop the reply").wait_for(timeout=30000)
        page.wait_for_timeout(5000)
        save(page, "live-thinking")
        page.get_by_title("Stop the reply").click()
    finally:
        finish_and_delete(page, thread)


@shot
def help_answer(page):
    thread = throwaway(page, B["atlas"], "Running zpool status without asking")
    try:
        open_page(page, f"/c/{thread}", height=1300)
        page.locator("form textarea").fill("How do I let you run zpool status without asking me every time?")
        page.get_by_title("Send (Enter)").click()
        page.get_by_title("Send (Enter)").wait_for(timeout=60000)
        page.wait_for_timeout(2000)
        save(page, "help-answer", page.locator(".dark-bubble-bot").last)
    finally:
        finish_and_delete(page, thread)


# ---- help --------------------------------------------------------------------------------------

@shot
def help_view(page):
    open_page(page, "/help/approvals")
    save(page, "help-view")


# ---- the computer (only when the stack runs the real runtime: screenshots.py --computer) ------

def computer_shot(func):
    func.needs_computer = True
    return shot(func)


@computer_shot
def computer_files(page):
    open_page(page, f"/p/{P['backup']}")
    page.get_by_role("button", name="Open in the file viewer").click()
    page.wait_for_timeout(2500)
    save(page, "computer-files")


def computer_action(page, bot, action, arguments):
    """Run an action on a bot's computer: opened, approved like any other, then run."""
    opened = page.request.post(f"{BASE}/api/v1/computers/{bot}/actions", data={"action": action, "arguments": arguments}).json()
    request_id = (opened.get("request") or {}).get("request_id")
    if opened.get("status") == "pending_approval" and request_id:
        page.request.post(f"{BASE}/api/v1/approvals/respond", data={"request_id": request_id, "action": "allow"})
        page.request.post(f"{BASE}/api/v1/computers/{bot}/actions/{request_id}/execute", timeout=120000)


@computer_shot
def computer_desktop(page):
    bot = B["atlas"]
    page.request.post(f"{BASE}/api/v1/computers/{bot}/start", timeout=180000)
    # The browser takes http(s) only: serve the seeded report from inside the computer, then open it.
    computer_action(page, bot, "terminal_execute", {"command": "cd /workspace/reports && python3 -m http.server 8088", "background": True})
    page.wait_for_timeout(1500)
    computer_action(page, bot, "browser_navigate", {"url": "http://localhost:8088/morning-report.html"})
    open_page(page, f"/c/{C['backup']}")
    page.locator("form textarea").fill("/computer")
    page.keyboard.press("Enter")
    page.wait_for_timeout(9000)  # the live view connects and draws the desktop
    save(page, "computer-desktop")


# ---- projects ------------------------------------------------------------------------------

@shot
def project_page(page):
    open_page(page, f"/p/{P['backup']}")
    save(page, "project-page")


@shot
def sidebar(page):
    open_page(page, f"/c/{C['backup']}")
    save(page, "sidebar", page.locator("aside, nav").first, pad=0)


# ---- bot settings --------------------------------------------------------------------------

BOT_SECTIONS = [("General", "general"), ("Model & thinking", "model"), ("Instructions", "instructions"), ("Notes & skills", "notes"),
                ("Tools", "tools"), ("Secrets", "secrets"), ("Schedules", "schedules"), ("Sharing", "sharing")]


@shot
def bot_settings(page):
    open_page(page, f"/c/{C['backup']}")
    page.get_by_title("Bot settings").click()
    page.wait_for_timeout(700)
    for label, name in BOT_SECTIONS:
        page.locator("nav").get_by_role("button", name=label).click()
        page.wait_for_timeout(900)
        save(page, f"bot-settings-{name}")


# ---- app settings and the other views ------------------------------------------------------

APP_SECTIONS = [("Account", "account"), ("Users", "users"), ("Model server", "model"), ("Auto-approval", "approval"), ("Connectors", "connectors"),
                ("Secrets", "secrets"), ("MCP servers", "mcp"), ("Skills", "skills")]


@shot
def app_settings(page):
    open_page(page, f"/c/{C['backup']}")
    page.get_by_title("Settings", exact=True).click()
    page.wait_for_timeout(800)
    for label, name in APP_SECTIONS:
        page.locator("nav").get_by_role("button", name=label).first.click()
        page.wait_for_timeout(900)
        save(page, f"app-settings-{name}")


@shot
def plugins(page):
    open_page(page, f"/c/{C['backup']}")
    page.get_by_text("Plugins", exact=True).click()
    page.wait_for_timeout(1500)
    save(page, "plugins")


@shot
def audit(page):
    open_page(page, f"/c/{C['backup']}")
    page.get_by_text("Audit trail", exact=True).click()
    page.wait_for_timeout(1500)
    save(page, "audit-trail")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    wanted = {name.strip() for name in args.only.split(",") if name.strip()}
    failures = []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel=args.channel, headless=True) if args.channel else p.chromium.launch(headless=True)

        def context_for(token):
            return browser.new_context(viewport=VIEWPORT, device_scale_factor=1, extra_http_headers={"Authorization": f"Bearer {token}"} if token else {})

        owner = context_for(seed["owner_token"])
        for func in SHOTS:
            if wanted and func.__name__ not in wanted:
                continue
            if getattr(func, "needs_computer", False) and not seed.get("computer"):
                print(func.__name__, "(skipped: run screenshots.py with --computer)")
                continue
            print(func.__name__)
            page = owner.new_page()
            try:
                func(page)
            except Exception as exc:  # one bad shot should not stop the rest
                failures.append(f"{func.__name__}: {exc}".splitlines()[0])
                print("  FAILED:", failures[-1])
            finally:
                page.close()

        # The sign-in screen, and the bot as a teammate it is shared with sees it.
        if not wanted or "login" in wanted:
            page = context_for(None).new_page()
            open_page(page, "/")
            save(page, "sign-in")
        if not wanted or "shared" in wanted:
            # Sam opens the app and picks Atlas under "Shared with me".
            page = context_for(seed["sam_token"]).new_page()
            open_page(page, "/")
            page.get_by_text("Atlas", exact=True).first.click()
            page.wait_for_timeout(1500)
            save(page, "shared-bot-teammate")
        browser.close()
    if failures:
        print("\nfailed shots:\n  " + "\n  ".join(failures))
        sys.exit(1)


main()

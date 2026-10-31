"""Re-take the docs screenshots: one command, after any change to the UI.

    python tools/docs/screenshots.py [--keep] [--computer] [--channel msedge] [--only overview,project_page]

It starts a throwaway Cubicle (docker compose project `cubicle-docs`, http://localhost:38080,
volumes of its own), seeds it with made-up data (seed_demo.py), photographs it into
docs/images/ (shoot.py) and removes it again, volumes and all. --keep leaves it running
to look around in (sign in as alex / demo-password-1234).

--computer adds the real computer runtime (compose.computer.yml) for the pictures of a
bot's desktop and files; the runtime image is built first if it is missing. The bot that
used a computer is deleted through the API at the end, which removes its container.

Needs Docker and Playwright (`pip install playwright`, then `playwright install chromium`,
or pass --channel msedge / chrome to use an installed browser). No model server is needed:
the stack talks to mock_llm.py, a scripted stand-in.
"""

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
COMPOSE = ["docker", "compose", "-p", "cubicle-docs", "-f", str(ROOT / "docker-compose.yml"), "-f", str(HERE / "compose.docs.yml")]
BASE = "http://localhost:38080"
SEED_FILE = HERE / ".seed.json"


def run(command, **kwargs):
    shown = ["docker", "compose"] + command[len(COMPOSE):] if command[:len(COMPOSE)] == COMPOSE else command
    print("$", " ".join(shown))
    return subprocess.run(command, cwd=ROOT, check=True, **kwargs)


def wait_for_api(timeout=240):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{BASE}/api/v1/health", timeout=3) as response:
                if response.status == 200:
                    return
        except Exception:
            pass
        time.sleep(2)
    raise SystemExit("The docs stack did not come up; see `docker compose -p cubicle-docs logs api`.")


RUNTIME_IMAGE = "cubicle-computer:2.14.0"


def remove_computers():
    """Delete the demo bots that started a computer, through the API, so their containers go too."""
    try:
        seed = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    except Exception:
        return
    for bot_id in seed.get("computer_bots") or []:
        request = urllib.request.Request(f"{BASE}/api/v1/bots/{bot_id}", method="DELETE", headers={"Authorization": f"Bearer {seed['owner_token']}"})
        try:
            urllib.request.urlopen(request, timeout=120).read()
        except Exception as exc:
            print(f"Could not delete {bot_id} (its computer may still be running): {exc}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true", help="leave the stack running afterwards")
    parser.add_argument("--channel", default=None)
    parser.add_argument("--only", default="")
    parser.add_argument("--no-build", action="store_true", help="use the images from the last run")
    parser.add_argument("--computer", action="store_true", help="add the real computer runtime for the desktop and file pictures")
    args = parser.parse_args()
    global COMPOSE
    if args.computer:
        COMPOSE = COMPOSE + ["-f", str(HERE / "compose.computer.yml"), "--profile", "computer"]
        if subprocess.run(["docker", "image", "inspect", RUNTIME_IMAGE], capture_output=True).returncode != 0:
            run(["docker", "build", "-t", RUNTIME_IMAGE, str(ROOT / "runtime")])

    run(COMPOSE + ["down", "-v", "--remove-orphans"], capture_output=True)
    run(COMPOSE + ["up", "-d"] + ([] if args.no_build else ["--build"]))
    try:
        wait_for_api()
        try:
            seeded = run(COMPOSE + ["exec", "-T", "api", "python", "-"], input=(HERE / "seed_demo.py").read_bytes(), capture_output=True)
        except subprocess.CalledProcessError as exc:
            # The seed's own error, not just the exit status.
            sys.stderr.write(exc.stderr.decode("utf-8", errors="replace"))
            raise
        line = seeded.stdout.decode("utf-8").strip().splitlines()[-1]
        json.loads(line)
        SEED_FILE.write_text(line, encoding="utf-8")
        shoot = [sys.executable, str(HERE / "shoot.py"), str(SEED_FILE), "--base", BASE]
        if args.channel:
            shoot += ["--channel", args.channel]
        if args.only:
            shoot += ["--only", args.only]
        run(shoot)
    finally:
        if args.computer and not args.keep:
            remove_computers()
        if args.keep:
            print(f"\nThe docs stack is still running at {BASE} (alex / demo-password-1234).")
            print("Remove it with: docker compose -p cubicle-docs down -v")
        else:
            run(COMPOSE + ["down", "-v", "--remove-orphans"], capture_output=True)


if __name__ == "__main__":
    main()

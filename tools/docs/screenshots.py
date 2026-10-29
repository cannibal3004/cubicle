"""Re-take the docs screenshots: one command, after any change to the UI.

    python tools/docs/screenshots.py [--keep] [--channel msedge] [--only overview,project_page]

It starts a throwaway Cubicle (docker compose project `cubicle-docs`, http://localhost:38080,
volumes of its own), seeds it with made-up data (seed_demo.py), photographs it into
docs/images/ (shoot.py) and removes it again, volumes and all. --keep leaves it running
to look around in (sign in as alex / demo-password-1234).

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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true", help="leave the stack running afterwards")
    parser.add_argument("--channel", default=None)
    parser.add_argument("--only", default="")
    parser.add_argument("--no-build", action="store_true", help="use the images from the last run")
    args = parser.parse_args()

    run(COMPOSE + ["down", "-v", "--remove-orphans"], capture_output=True)
    run(COMPOSE + ["up", "-d"] + ([] if args.no_build else ["--build"]))
    try:
        wait_for_api()
        seeded = run(COMPOSE + ["exec", "-T", "api", "python", "-"], input=(HERE / "seed_demo.py").read_bytes(), capture_output=True)
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
        if args.keep:
            print(f"\nThe docs stack is still running at {BASE} (alex / demo-password-1234).")
            print("Remove it with: docker compose -p cubicle-docs down -v")
        else:
            run(COMPOSE + ["down", "-v", "--remove-orphans"], capture_output=True)


if __name__ == "__main__":
    main()

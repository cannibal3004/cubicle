"""Check the docs' links: every relative link and image resolves, and every #anchor names a heading.

    python tools/docs/check_links.py [README.md docs]
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LINK = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)\)")


def slug(heading: str) -> str:
    """GitHub's anchor for a heading."""
    text = re.sub(r"[`*_]", "", heading.strip().lower())
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def anchors(path: Path) -> set:
    found = set()
    in_code = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("```"):
            in_code = not in_code
        elif not in_code and line.startswith("#"):
            found.add(slug(line.lstrip("#")))
    return found


def check(files):
    problems = []
    for page in files:
        text = page.read_text(encoding="utf-8")
        text = re.sub(r"```.*?```", "", text, flags=re.S)
        for target in LINK.findall(text):
            if re.match(r"^[a-z]+:", target):
                continue
            file_part, _, anchor = target.partition("#")
            dest = (page.parent / file_part).resolve() if file_part else page
            if not dest.exists():
                problems.append(f"{page.relative_to(ROOT)}: missing {target}")
            elif anchor and dest.suffix == ".md" and anchor not in anchors(dest):
                problems.append(f"{page.relative_to(ROOT)}: no heading for #{anchor} in {dest.relative_to(ROOT)}")
    return problems


def main():
    targets = [ROOT / arg for arg in sys.argv[1:]] or [ROOT / "README.md", ROOT / "docs"]
    files = []
    for target in targets:
        files += sorted(target.rglob("*.md")) if target.is_dir() else [target]
    problems = check(files)
    print("\n".join(problems) if problems else f"{len(files)} pages, all links resolve")
    sys.exit(1 if problems else 0)


main()

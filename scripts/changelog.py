"""Print one version's CHANGELOG.md entry, which becomes that release's notes.

    python3 scripts/changelog.py 0.1.0      # the entry (a leading v is fine)
    python3 scripts/changelog.py --check    # every reachable v* tag has an entry, and [Unreleased] is there

A tag with no entry, or an empty one, fails here rather than publishing notes nobody wrote.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

TEXT = (Path(__file__).resolve().parent.parent / "CHANGELOG.md").read_text()
HEADINGS = list(re.finditer(r"^## \[(\d+\.\d+\.\d+)\] - \d{4}-\d{2}-\d{2}$", TEXT, re.MULTILINE))


def entry(version: str) -> str:
    for index, heading in enumerate(HEADINGS):
        if heading[1] == version:
            end = HEADINGS[index + 1].start() if index + 1 < len(HEADINGS) else len(TEXT)
            body = TEXT[heading.end() : end].strip()
            if not body:
                sys.exit(f"CHANGELOG.md's entry for {version} is empty")
            return body
    sys.exit(f"CHANGELOG.md has no entry for {version}")


def check() -> None:
    if not re.search(r"^## \[Unreleased\]$", TEXT, re.MULTILINE):
        sys.exit("CHANGELOG.md has no ## [Unreleased] heading")
    tags = subprocess.run(
        ["git", "tag", "--merged", "HEAD", "--list", "v*"], capture_output=True, text=True, check=True
    ).stdout.split()
    for tag in tags:
        if re.fullmatch(r"v\d+\.\d+\.\d+", tag):
            entry(tag[1:])


def main() -> None:
    match sys.argv[1:]:
        case ["--check"]:
            check()
        case [version]:
            print(entry(version.removeprefix("v")))
        case _:
            sys.exit("usage: python3 scripts/changelog.py <version> | --check")


if __name__ == "__main__":
    main()

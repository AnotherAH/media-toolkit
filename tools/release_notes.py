"""Print the CHANGELOG.md section for one version, for the GitHub release.

    python tools/release_notes.py 1.2.0 > notes.md
    python tools/release_notes.py 1.2.0 notes.md      write the file directly

Writing the file directly keeps it UTF-8 whatever the console's code page;
the release workflow does that.

Accepts headings like "## 1.2.0", "## [1.2.0] - 2026-09-30" or
"## Media Toolkit 1.2.0". The section runs until the next heading of the same
or a higher level. Exits with an error when there is no such section, so a
release can never go out with empty notes.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def section(text: str, version: str) -> str:
    lines = text.splitlines()
    start = level = None
    pattern = re.compile(rf"^(#+)\s.*(?<![\w.]){re.escape(version)}(?![\w.])")
    for i, line in enumerate(lines):
        m = pattern.match(line)
        if m:
            start, level = i + 1, len(m.group(1))
            break
    if start is None:
        raise LookupError(f"CHANGELOG.md has no section for {version}")
    end = len(lines)
    for j in range(start, len(lines)):
        m = re.match(r"^(#+)\s", lines[j])
        if m and len(m.group(1)) <= level:
            end = j
            break
    body = "\n".join(lines[start:end]).strip()
    if not body:
        raise LookupError(f"The CHANGELOG.md section for {version} is empty")
    return body + "\n"


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 3):
        print(__doc__, file=sys.stderr)
        return 2
    try:
        text = (ROOT / "CHANGELOG.md").read_text("utf-8")
        notes = section(text, argv[1].lstrip("vV"))
        if len(argv) == 3:
            Path(argv[2]).write_text(notes, encoding="utf-8")
        else:
            sys.stdout.write(notes)
    except (OSError, LookupError) as exc:
        print(f"release_notes: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

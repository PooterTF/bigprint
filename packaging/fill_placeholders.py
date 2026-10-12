# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 YOUR NAME
"""Fill in the project's placeholders (GitHub username, donation pages, name).

    python packaging/fill_placeholders.py --github alice --kofi alice3d --bmc alice3d --name "Alice Smith"
    python packaging/fill_placeholders.py --check     # list any placeholders still left

Updates every text file in the repository (README, LICENSE headers,
pyproject.toml, .github/FUNDING.yml, bigprint/project.py, ...). Run it once
before publishing; the release workflow refuses to build while any remain.
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", ".venv", "build", "dist", "__pycache__", ".pytest_cache", "vendor"}
TEXT_SUFFIXES = {".py", ".md", ".toml", ".yml", ".yaml", ".txt", ".html", ".spec", ".ps1", ".cfg"}
PLACEHOLDERS = {
    "github": "YOUR-GITHUB-USERNAME",
    "kofi": "YOUR-KOFI-NAME",
    "bmc": "YOUR-BMC-NAME",
    "name": "YOUR NAME",
}
SELF = Path(__file__).resolve()


def text_files():
    for path in ROOT.rglob("*"):
        if path.is_file() and path.suffix in TEXT_SUFFIXES and path.resolve() != SELF:
            if not SKIP_DIRS.intersection(path.relative_to(ROOT).parts) and not path.name.endswith(".egg-info"):
                yield path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--github", help="GitHub username or organization hosting the repository")
    ap.add_argument("--kofi", help="Ko-fi page name (ko-fi.com/<name>)")
    ap.add_argument("--bmc", help="Buy Me a Coffee page name (buymeacoffee.com/<name>)")
    ap.add_argument("--name", help="Name for the copyright lines")
    ap.add_argument("--check", action="store_true", help="Only report placeholders that are still unfilled")
    args = ap.parse_args()

    values = {k: getattr(args, k) for k in PLACEHOLDERS if getattr(args, k)}
    left: dict[str, list[str]] = {}
    for path in text_files():
        text = path.read_text(encoding="utf-8")
        new = text
        for key, value in values.items():
            new = new.replace(PLACEHOLDERS[key], value)
        if new != text and not args.check:
            path.write_text(new, encoding="utf-8", newline="")
            print(f"updated {path.relative_to(ROOT)}")
        for key, token in PLACEHOLDERS.items():
            if token in new:
                left.setdefault(token, []).append(str(path.relative_to(ROOT)))

    if left:
        print("\nPlaceholders still to fill in:")
        for token, files in left.items():
            print(f"  {token}: {', '.join(sorted(files))}")
        return 1 if args.check else 0
    print("All placeholders are filled in.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

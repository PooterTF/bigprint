# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Michael Christopher Stone
"""Copy the license files of every bundled package into dist/bigprint/licenses/.

Run by build_windows.ps1 after PyInstaller. The package list is bigprint's
runtime dependency tree (requirements of requirements, extras ignored), which
is what PyInstaller bundles.
"""

import re
import shutil
import sys
from importlib import metadata
from pathlib import Path

dist = Path(sys.argv[1])
out = dist / "licenses"
out.mkdir(exist_ok=True)


def runtime_closure(root: str) -> list[metadata.Distribution]:
    seen: dict[str, metadata.Distribution] = {}
    todo = [root]
    while todo:
        name = re.split(r"[ ;<>=!~\[(]", todo.pop(), maxsplit=1)[0].strip().lower().replace("_", "-")
        if not name or name in seen:
            continue
        try:
            d = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            continue
        seen[name] = d
        for req in d.requires or []:
            if "extra ==" in req:
                continue
            marker = req.split(";", 1)[1] if ";" in req else ""
            if "sys_platform" in marker and "win32" not in marker and "!=" not in marker:
                continue
            todo.append(req)
    return [d for n, d in sorted(seen.items()) if n != root]


def license_of(meta) -> str:
    lic = meta.get("License-Expression") or meta.get("License") or ""
    if lic and len(lic) <= 80:
        return lic
    classifiers = [c.split(" :: ")[-1] for c in meta.get_all("Classifier") or [] if c.startswith("License ::")]
    return ", ".join(classifiers) or "see license files"


lines = ["Third-party software bundled with bigprint", "=" * 42, ""]
for d in runtime_closure("bigprint"):
    meta = d.metadata
    lines.append(f"{meta['Name']} {meta['Version']}: {license_of(meta)}")
    for f in d.files or []:
        if any(k in f.name.upper() for k in ("LICENSE", "LICENCE", "COPYING", "NOTICE", "AUTHORS")):
            src = Path(d.locate_file(f))
            if src.is_file():
                shutil.copy(src, out / f"{meta['Name']}-{f.name}")

lines += [
    "",
    f"Python {sys.version.split()[0]}: PSF License (Python-LICENSE.txt)",
    "three.js r160: MIT (three.js-LICENSE.txt)",
]
(out / "THIRD-PARTY-NOTICES.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
python_license = Path(sys.base_prefix) / "LICENSE.txt"
if python_license.exists():
    shutil.copy(python_license, out / "Python-LICENSE.txt")
three = Path(__file__).resolve().parent.parent / "bigprint" / "static" / "vendor" / "three" / "LICENSE.txt"
shutil.copy(three, out / "three.js-LICENSE.txt")
print("\n".join(lines))

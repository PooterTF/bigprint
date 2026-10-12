# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Michael Christopher Stone
"""Write a small test model in every input format bigprint accepts.

Used by build_windows.ps1 to check the packaged program can actually read each
format: some format readers depend on libraries that are imported lazily and
easy to leave out of a build.
"""

import sys
from pathlib import Path

import trimesh

FORMATS = ("stl", "obj", "ply", "3mf")

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
model = trimesh.creation.icosphere(subdivisions=3, radius=50)
for ext in FORMATS:
    model.export(str(out / f"sample.{ext}"))
    print(out / f"sample.{ext}")

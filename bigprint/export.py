# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Michael Christopher Stone
"""Write a finished Result to an output folder (shared by the CLI and GUI)."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from .io import save_stl
from .pipeline import Result
from .preview import write_preview
from .report import write_report
from .threemf import write_3mf


def write_outputs(
    result: Result,
    out: Path,
    *,
    price_per_kg: float | None = None,
    preview: bool = True,
    pieces: bool = True,
    progress: Callable[[float, str], None] | None = None,
) -> list[Path]:
    """Write report, preview, and (unless `pieces` is False) the print files.

    Returns the paths written.
    """
    tick = progress or (lambda *_: None)
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    written = [out / "report.md"]
    write_report(result, written[0], price_per_kg=price_per_kg)
    if preview:
        tick(0.02, "Writing preview")
        write_preview(result, out / "preview.html")
        written.append(out / "preview.html")
    if not pieces:
        return written

    pieces_dir = out / "pieces"
    pieces_dir.mkdir(exist_ok=True)
    objects = []
    n = len(result.pieces)
    for i, fp in enumerate(result.pieces):
        tick(0.05 + 0.75 * i / max(n, 1), f"Writing piece {fp.id}")
        printable = fp.printable()
        path = pieces_dir / f"{fp.id}.stl"
        save_stl(printable, path)
        written.append(path)
        f = fp.fill
        objects.append(
            {
                "name": fp.id,
                "manifold": printable,
                "settings": {
                    "perimeters": f.walls,
                    "fill_density": f.infill_pct,
                    "fill_pattern": f.pattern,
                    "top_solid_layers": f.top_layers,
                    "bottom_solid_layers": f.bottom_layers,
                },
            }
        )
    tick(0.82, "Writing project.3mf")
    write_3mf(out / "project.3mf", objects)
    written.append(out / "project.3mf")
    if result.pins is not None:
        tick(0.95, "Writing dowels.stl")
        save_stl(result.pins, out / "dowels.stl")
        written.append(out / "dowels.stl")
    tick(1.0, "Done")
    return written

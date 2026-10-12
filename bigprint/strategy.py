# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Michael Christopher Stone
"""Per-piece fill strategy and filament estimates.

Three modes:
- hollow: geometric shell (see hollow.py), printed with enough walls to fill
  the shell; the thicker joint collars get the preset's infill.
  Cheapest by far for large organic parts.
- sparse: solid geometry, the preset's walls plus sparse infill.
- solid: thin parts that are mostly walls anyway.

The slicer wraps perimeters around dowel holes automatically, so holes stay
strong in low-infill parts. Presets are deliberately few and fixed; anything
finer is better tuned in the slicer.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from manifold3d import Manifold


@dataclass(frozen=True)
class Preset:
    label: str
    walls: int
    infill: float  # 0..1
    pattern: str
    top_layers: int
    bottom_layers: int
    why: str

    @property
    def infill_pct(self) -> str:
        return f"{round(self.infill * 100)}%"

    def describe(self) -> str:
        return (
            f"{self.walls} walls, {self.infill_pct} {self.pattern} infill, "
            f"{self.top_layers} top / {self.bottom_layers} bottom layers"
        )


# Tuned for large FDM parts with a 0.4 mm nozzle and 0.2 mm layers. Gyroid
# throughout: pieces are printed in many orientations and gyroid is about
# equally strong in every direction.
PRESETS: dict[str, Preset] = {
    "display": Preset(
        "Display piece", 3, 0.08, "gyroid", 6, 4,
        "Lightest that prints cleanly: the walls carry the shape, and 6 top layers "
        "bridge the sparse infill without sagging or pillowing.",
    ),
    "wearable": Preset(
        "Wearable / handled", 4, 0.15, "gyroid", 5, 4,
        "Survives knocks and handling; the fourth wall adds most of the impact strength.",
    ),
    "structural": Preset(
        "Structural", 6, 0.25, "gyroid", 6, 5,
        "Carries load: strength comes mostly from walls, so walls go up more than infill.",
    ),
}
PURPOSES = PRESETS  # name kept for callers that only need the keys

MATERIAL_DENSITY = {"pla": 1.24, "petg": 1.27, "abs": 1.04, "asa": 1.07, "tpu": 1.21}


@dataclass
class FillPlan:
    mode: str  # "hollow", "sparse" or "solid"
    walls: int
    infill: float  # 0..1
    pattern: str
    top_layers: int
    bottom_layers: int
    grams: float
    grams_solid: float
    note: str = ""

    @property
    def infill_pct(self) -> str:
        return f"{round(self.infill * 100)}%"


def hollow_walls(preset: Preset, wall: float, nozzle: float) -> int:
    """Perimeters that fill a hollow shell of thickness `wall`."""
    return max(preset.walls, math.ceil(wall / (nozzle * 1.125)))


def plan_fill(
    solid: Manifold,
    *,
    purpose: str = "display",
    nozzle: float = 0.4,
    layer_height: float = 0.2,
    density: float = 1.24,
    hollow_wall: float | None = None,
    solid_volume: float | None = None,
) -> FillPlan:
    """Fill settings for a piece.

    hollow_wall: set when the piece is a geometric shell of that thickness.
    solid_volume: volume before hollowing, for the "printed solid" comparison.
    """
    p = PRESETS[purpose]
    line_w = nozzle * 1.125

    volume = solid.volume()  # mm^3
    area = solid.surface_area()  # mm^2
    grams_solid = (solid_volume or volume) / 1000 * density

    if hollow_wall is not None:
        walls = hollow_walls(p, hollow_wall, nozzle)
        shell_vol = min(volume, area / 2 * walls * line_w)
        grams = (shell_vol + (volume - shell_vol) * p.infill) / 1000 * density
        return FillPlan(
            "hollow", walls, p.infill, p.pattern, p.top_layers, p.bottom_layers, grams, grams_solid,
            note=f"hollow shell, saves {(1 - grams / grams_solid) * 100:.0f}% vs solid",
        )

    shell_t = p.walls * line_w
    # Shell volume ~ area * thickness, averaging wall and top/bottom skin
    # thickness. Capped at the part's volume.
    skin_t = max(shell_t, (p.top_layers + p.bottom_layers) / 2 * layer_height)
    shell_vol = min(volume, area * (shell_t + skin_t) / 2)
    grams_sparse = (shell_vol + (volume - shell_vol) * p.infill) / 1000 * density

    # Characteristic thickness: a slab of thickness t has 2V/A ~= t.
    thickness = 2 * volume / area if area > 0 else 0.0
    if thickness < 3 * shell_t or grams_sparse > 0.85 * grams_solid:
        return FillPlan(
            "solid", p.walls, 1.0, "rectilinear", p.top_layers, p.bottom_layers, grams_solid, grams_solid,
            note=f"thin part (~{thickness:.0f} mm): mostly walls anyway, print solid for strength",
        )
    return FillPlan(
        "sparse", p.walls, p.infill, p.pattern, p.top_layers, p.bottom_layers, grams_sparse, grams_solid,
        note=f"saves {(1 - grams_sparse / grams_solid) * 100:.0f}% vs solid",
    )


def fill_basis(purpose: str, modes: dict[str, int], wall: float, nozzle: float) -> str:
    """One sentence on what the filament estimate assumes."""
    p = PRESETS[purpose]
    parts = [f"{p.label} preset: {p.describe()}."]
    if modes.get("hollow"):
        n = modes["hollow"]
        parts.append(
            f"{n} hollow piece{'s' if n != 1 else ''}: {wall:g} mm shells "
            f"({hollow_walls(p, wall, nozzle)} walls fill the shell)."
        )
    if modes.get("solid"):
        n = modes["solid"]
        parts.append(f"{n} thin piece{'s' if n != 1 else ''} print{'s' if n == 1 else ''} solid.")
    return " ".join(parts)

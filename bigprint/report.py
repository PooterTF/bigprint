# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Michael Christopher Stone
"""Markdown project report: pieces, settings, filament, dowels, assembly map."""

from __future__ import annotations

from pathlib import Path

from .pipeline import Result
from .split import AXES


def write_report(result: Result, path: str | Path, price_per_kg: float | None = None) -> None:
    o = result.options
    total_g = sum(fp.fill.grams for fp in result.pieces)
    solid_g = sum(fp.fill.grams_solid for fp in result.pieces)
    lines = [
        f"# bigprint report — {Path(result.source).name}",
        "",
        f"- Scale factor: **{result.scale_factor:.3f}**",
        f"- Final size: **{result.size[0]:.0f} × {result.size[1]:.0f} × {result.size[2]:.0f} mm** (X × Y × Z)",
        f"- Bed: {o.bed[0]:g} × {o.bed[1]:g} × {o.bed[2]:g} mm (margin {o.margin:g} mm)",
        f"- Cut planes: {result.plan.describe()}",
        f"- Pieces: **{len(result.pieces)}**",
        f"- Estimated filament: **{total_g / 1000:.2f} kg** (vs {solid_g / 1000:.2f} kg printed solid)"
        + (f" ≈ {total_g / 1000 * price_per_kg:.2f} at {price_per_kg:g}/kg" if price_per_kg else ""),
        "",
        f"Estimate based on: {result.fill_basis} Filament figures are for planning, not slicer-exact.",
        "",
        "## Pieces",
        "",
        "| ID | Print size (mm) | On bed | Fill | Est. g | Solid g | Touches | Notes |",
        "|---|---|---|---|---:|---:|---|---|",
    ]
    for fp in result.pieces:
        s = fp.placement.size
        bed = fp.placement.bed_face
        bed_txt = f"{'+' if bed[1] > 0 else '-'}{AXES[bed[0]].upper()} cut face" if bed else "as modelled"
        fill = fp.fill
        fill_txt = "solid (100%)" if fill.mode == "solid" else f"{fill.walls} walls, {fill.infill_pct} {fill.pattern}"
        nb = ", ".join(sorted(set(fp.piece.neighbors))) or "—"
        lines.append(
            f"| {fp.id} | {s[0]:.0f}×{s[1]:.0f}×{s[2]:.0f} | {bed_txt} | {fill_txt} | {fill.grams:.0f} | "
            f"{fill.grams_solid:.0f} | {nb} | {fill.note} |"
        )

    lines += ["", "## Connectors", ""]
    if o.dowels and result.pin_counts:
        d = o.dowel
        n = sum(result.pin_counts.values())
        lines.append(
            f"{n} dowel pins, Ø{d.diameter:g} mm, in Ø{d.diameter + d.clearance:g} mm holes. "
            "Print `dowels.stl` (pins lie flat for strength) or use off-the-shelf dowels cut to length:"
        )
        lines.append("")
        for depth, count in sorted(result.pin_counts.items()):
            lines.append(f"- {count} × {2 * depth:g} mm long")
        lines.append("")
        lines.append("| Joint | Plane | Dowels |")
        lines.append("|---|---|---:|")
        for j in result.joints:
            lines.append(f"| {j.neg} ↔ {j.pos} | {AXES[j.axis]} = {j.coord:.1f} | {len(j.dowels)} |")
    else:
        lines.append("Dowels disabled — glue only.")

    lines += ["", "## Assembly", ""]
    lines.append(
        "Piece IDs are debossed on a mating face (hidden once assembled). The letter is the "
        "layer from the bottom (A = lowest); the number orders pieces within that layer. "
        "Dry-fit each joint with pins before gluing. Open `preview.html` to see the exploded layout."
    )
    if result.warnings:
        lines += ["", "## Warnings", ""] + [f"- {w}" for w in result.warnings]

    lines += [
        "",
        "## Slicer notes",
        "",
        "- `project.3mf` carries per-piece perimeters/infill for PrusaSlicer. Other slicers load the "
        "geometry; apply the Fill column manually.",
        "- Pieces are oriented with their largest cut face on the bed where possible; add supports "
        "for any remaining overhangs.",
        "- All pieces will not fit on one plate — use Arrange / multiple plates.",
    ]
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")

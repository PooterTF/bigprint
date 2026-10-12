# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Michael Christopher Stone
"""Command-line interface: `bigprint MODEL.stl --height 1.8m --printer bambu-x1`."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Optional

import typer

from .connectors import DowelSpec
from .io import MeshError
from .printers import DEFAULT_PRINTER, PRINTERS
from .scale import parse_length
from .strategy import MATERIAL_DENSITY, PURPOSES

app = typer.Typer(add_completion=False, help="Scale a model to life size and split it into printable, pinned pieces.")


def _parse_bed(text: str) -> tuple[float, float, float]:
    parts = re.split(r"[x×,\s]+", text.strip().lower())
    if len(parts) != 3:
        raise typer.BadParameter("Bed must be WxDxH, e.g. 256x256x256")
    return tuple(parse_length(p) for p in parts)  # type: ignore[return-value]


def _parse_cuts(text: str) -> dict[int, list[float]]:
    """'x=100,200 z=500' -> {0: [100, 200], 2: [500]}"""
    out: dict[int, list[float]] = {}
    for chunk in re.split(r"[;\s]+", text.strip()):
        if not chunk:
            continue
        axis, _, vals = chunk.partition("=")
        if axis.lower() not in "xyz" or not vals:
            raise typer.BadParameter(f"Bad cut spec {chunk!r}; use e.g. 'x=100,200 z=500'")
        out["xyz".index(axis.lower())] = [parse_length(v) for v in vals.split(",") if v]
    return out


def _version(value: bool) -> None:
    if not value:
        return
    from . import __version__
    from .project import LICENSE, SOURCE_URL, SUPPORT_LINKS

    typer.echo(f"bigprint {__version__}  (free software, {LICENSE})")
    typer.echo(f"Source code: {SOURCE_URL}")
    typer.echo("Support bigprint: " + "  |  ".join(f"{k}: {v}" for k, v in SUPPORT_LINKS.items()))
    raise typer.Exit()


@app.command()
def main(
    model: Path = typer.Argument(..., exists=True, dir_okay=False, help="Input STL/OBJ/3MF/PLY"),
    version: bool = typer.Option(
        False, "--version", callback=_version, is_eager=True, help="Show version, license, source and support links"
    ),
    out: Path = typer.Option(None, "--out", "-o", help="Output folder (default: <model>_bigprint)"),
    height: Optional[str] = typer.Option(None, help="Target height (Z), e.g. 1.8m, 180cm, 6ft"),
    width: Optional[str] = typer.Option(None, help="Target width (X)"),
    depth: Optional[str] = typer.Option(None, help="Target depth (Y)"),
    scale: Optional[float] = typer.Option(None, help="Uniform scale factor"),
    smooth: bool = typer.Option(False, help="Smooth out the flat facets of low-poly models after scaling"),
    smooth_angle: float = typer.Option(60.0, help="With --smooth: edges sharper than this angle (deg) stay crisp"),
    smooth_edge: float = typer.Option(5.0, help="With --smooth: target triangle size (mm); smaller is smoother but slower"),
    up: str = typer.Option("z", help="Which model axis is up: z, y, -y, x, -z"),
    printer: Optional[str] = typer.Option(None, help=f"Printer preset: {', '.join(PRINTERS)}"),
    bed: Optional[str] = typer.Option(None, help="Build volume WxDxH in mm, e.g. 256x256x256"),
    margin: float = typer.Option(5.0, help="Safety margin subtracted from bed size (mm)"),
    cuts: Optional[str] = typer.Option(None, help="Manual cut planes, e.g. 'x=100,200 z=500' (mm)"),
    even_cuts: bool = typer.Option(False, help="Space cuts evenly instead of seeking narrow sections"),
    dowels: bool = typer.Option(True, help="Add dowel-pin holes on joints"),
    dowel_d: float = typer.Option(6.0, help="Dowel diameter (mm)"),
    dowel_depth: float = typer.Option(15.0, help="Dowel insertion depth per side (mm)"),
    clearance: float = typer.Option(0.4, help="Hole diameter minus dowel diameter (mm)"),
    labels: bool = typer.Option(True, help="Deboss piece IDs on a mating face"),
    repair: bool = typer.Option(True, help="Automatically rebuild damaged models (holes, overlapping surfaces)"),
    merge_small: bool = typer.Option(True, help="Fuse small fragments into a neighbour when it still fits"),
    hollow: str = typer.Option("auto", help="Hollow shells: auto (when it saves filament), on, off"),
    wall: float = typer.Option(4.0, help="Hollow shell wall thickness (mm)"),
    purpose: str = typer.Option(
        "display", help="Strength preset: " + "; ".join(f"{k} = {p.describe()}" for k, p in PURPOSES.items())
    ),
    material: str = typer.Option("pla", help=f"Material for weight estimate: {', '.join(MATERIAL_DENSITY)}"),
    nozzle: float = typer.Option(0.4, help="Nozzle diameter (mm)"),
    price: Optional[float] = typer.Option(None, help="Filament price per kg, for a cost estimate"),
    preview: bool = typer.Option(True, help="Write preview.html"),
    dry_run: bool = typer.Option(False, help="Plan and report only; don't write piece files"),
):
    from .export import write_outputs
    from .pipeline import Options, run

    if printer and bed:
        raise typer.BadParameter("Use either --printer or --bed, not both")
    if printer:
        if printer not in PRINTERS:
            raise typer.BadParameter(f"Unknown printer {printer!r}. Known: {', '.join(PRINTERS)}")
        bed_size = tuple(float(v) for v in PRINTERS[printer])
    elif bed:
        bed_size = _parse_bed(bed)
    else:
        bed_size = tuple(float(v) for v in PRINTERS[DEFAULT_PRINTER])
        typer.echo("No --printer/--bed given; assuming 256x256x256 mm")
    if purpose not in PURPOSES:
        raise typer.BadParameter(f"--purpose must be one of {', '.join(PURPOSES)}")
    if hollow not in ("auto", "on", "off"):
        raise typer.BadParameter("--hollow must be auto, on or off")
    if material.lower() not in MATERIAL_DENSITY:
        raise typer.BadParameter(f"--material must be one of {', '.join(MATERIAL_DENSITY)}")

    opt = Options(
        bed=bed_size,  # type: ignore[arg-type]
        margin=margin,
        up=up,
        scale=scale,
        height=parse_length(height) if height else None,
        width=parse_length(width) if width else None,
        depth=parse_length(depth) if depth else None,
        smooth=smooth,
        smooth_angle=smooth_angle,
        smooth_edge=smooth_edge,
        optimise_cuts=not even_cuts,
        manual_cuts=_parse_cuts(cuts) if cuts else None,
        dowels=dowels,
        dowel=DowelSpec(diameter=dowel_d, depth=dowel_depth, clearance=clearance),
        labels=labels,
        repair=repair,
        merge_small=merge_small,
        hollow=hollow,
        wall=wall,
        purpose=purpose,
        nozzle=nozzle,
        density=MATERIAL_DENSITY[material.lower()],
    )

    try:
        result = run(str(model), opt, log=typer.echo)
    except (MeshError, ValueError) as e:
        typer.secho(f"error: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)

    out = out or model.with_name(model.stem + "_bigprint")
    write_outputs(result, out, price_per_kg=price, preview=preview, pieces=not dry_run)

    total = sum(fp.fill.grams for fp in result.pieces)
    typer.echo(f"\n{len(result.pieces)} pieces, ~{total / 1000:.2f} kg filament -> {out}")
    typer.echo(f"Estimate based on: {result.fill_basis}")
    for w in result.warnings:
        typer.secho(f"warning: {w}", fg=typer.colors.YELLOW)


if __name__ == "__main__":
    sys.exit(app())

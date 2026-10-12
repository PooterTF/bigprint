# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Michael Christopher Stone
"""End-to-end: load -> scale -> plan cuts -> cut cells -> hollow -> pieces ->
connectors -> orientation -> labels -> fill plan."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from manifold3d import Manifold, OpType

from .connectors import DowelSpec, pin_plate, place_dowels
from .io import NeedsRepair, clean, load_model, survives_stl
from .labels import label_cutters
from .orient import Placement, choose_placement
from .repair import choose_pitch, repaired_pitch_note, voxel_repair
from .scale import extents, orient_up, oriented_extents, scale_model, target_factor
from .smooth import smooth_model
from .hollow import HollowSpec, hollow_cell
from .split import (
    CutPlan, Joint, Piece, assign_ids, cut_cells, find_joints, link_neighbors, make_pieces, merge_small_pieces, plan_cuts,
)
from .strategy import FillPlan, fill_basis, plan_fill


@dataclass
class Options:
    bed: tuple[float, float, float] = (256.0, 256.0, 256.0)
    margin: float = 5.0
    up: str = "z"
    scale: float | None = None
    height: float | None = None
    width: float | None = None
    depth: float | None = None
    smooth: bool = False
    smooth_angle: float = 60.0  # edges sharper than this stay crisp
    smooth_edge: float = 5.0  # target triangle size after smoothing, mm
    optimise_cuts: bool = True
    manual_cuts: dict[int, list[float]] | None = None
    dowels: bool = True
    dowel: DowelSpec = field(default_factory=DowelSpec)
    labels: bool = True
    repair: bool = True  # rebuild damaged models automatically
    merge_small: bool = True
    hollow: str = "auto"  # auto | on | off
    wall: float = 4.0  # hollow shell thickness
    purpose: str = "display"
    nozzle: float = 0.4
    layer_height: float = 0.2
    density: float = 1.24


@dataclass
class FinishedPiece:
    piece: Piece
    final: Manifold  # model frame, with holes and label
    placement: Placement
    fill: FillPlan

    @property
    def id(self) -> str:
        return self.piece.id

    def printable(self) -> Manifold:
        return self.placement.apply(self.final)


@dataclass
class Result:
    options: Options
    source: str
    scale_factor: float
    size: tuple[float, float, float]
    plan: CutPlan
    pieces: list[FinishedPiece]
    joints: list[Joint]
    pins: Manifold | None
    pin_counts: dict[float, int]
    warnings: list[str]
    dropped_cells: int = 0  # interior cells that hollowing removed entirely

    @property
    def fill_basis(self) -> str:
        """What the filament estimate assumes (preset settings, hollow/solid pieces)."""
        modes: dict[str, int] = {}
        for fp in self.pieces:
            modes[fp.fill.mode] = modes.get(fp.fill.mode, 0) + 1
        o = self.options
        return fill_basis(o.purpose, modes, o.wall, o.nozzle)


def run(path: str, opt: Options, log=print, progress: Callable[[float, str], None] | None = None) -> Result:
    """Run the whole pipeline.

    `progress(fraction, stage)` is called as work advances (0..1). A caller can
    abort the run by raising an exception from it, e.g. when the user cancels.
    """
    tick = progress or (lambda *_: None)
    tick(0.0, "Loading model")
    log(f"Loading {path}")
    try:
        base = load_model(path, log=log)
    except NeedsRepair as e:
        if not opt.repair:
            raise
        ext = oriented_extents(e.mesh.extents, opt.up)
        f = target_factor(ext, factor=opt.scale, height=opt.height, width=opt.width, depth=opt.depth)
        pitch, final_pitch = choose_pitch(e.mesh.extents, f)
        log(f"Damaged model ({e.problem}); repairing automatically, {repaired_pitch_note(final_pitch)}")
        base = voxel_repair(e.mesh, pitch, progress=lambda fr, st: tick(0.01 + 0.06 * fr, st))
        log(f"Repaired: closed surface with {base.num_tri():,} triangles")
    model = orient_up(base, opt.up)
    model, factor = scale_model(model, factor=opt.scale, height=opt.height, width=opt.width, depth=opt.depth)
    size = extents(model)
    log(f"Scaled x{factor:.3f} -> {size[0]:.0f} x {size[1]:.0f} x {size[2]:.0f} mm")
    if opt.smooth:
        tick(0.08, "Smoothing")
        before = model.num_tri()
        model = smooth_model(model, opt.smooth_angle, opt.smooth_edge)
        log(f"Smoothed: {before} -> {model.num_tri()} triangles (edges sharper than {opt.smooth_angle:g} deg kept)")

    tick(0.10, "Planning cuts")
    plan = plan_cuts(model, opt.bed, margin=opt.margin, optimise=opt.optimise_cuts, manual=opt.manual_cuts)
    log(f"Cuts: {plan.describe()}")
    cells = cut_cells(model, plan)

    # Hollow each cell; remember its pre-hollowing volume for the solid comparison.
    solid_volume: dict[tuple, float] = {c.index: c.solid.volume() for c in cells}
    dropped = 0
    if opt.hollow != "off":
        spec = HollowSpec.for_dowels(opt.wall, opt.dowel)
        log(f"Hollowing {len(cells)} cells ({opt.wall:g} mm walls, {spec.collar:.1f} mm joint collars)")
        kept = []
        for i, cell in enumerate(cells):
            tick(0.12 + 0.48 * i / len(cells), f"Hollowing cell {i + 1} of {len(cells)}")
            shell = hollow_cell(model, cell, plan, spec)
            if shell is not None and opt.hollow == "auto":
                sparse_g = plan_fill(cell.solid, purpose=opt.purpose, nozzle=opt.nozzle, density=opt.density).grams
                hollow_g = plan_fill(shell, purpose=opt.purpose, nozzle=opt.nozzle, density=opt.density,
                                     hollow_wall=opt.wall).grams if not shell.is_empty() else 0.0
                if hollow_g > 0.8 * sparse_g:
                    shell = None
            if shell is not None:
                cell.solid, cell.hollow = shell, True
                if shell.is_empty() or shell.volume() < 1.0:
                    dropped += 1
                    continue
            kept.append(cell)
        cells = kept
        if dropped:
            log(f"{dropped} interior cell(s) fall entirely inside the hollow and need no printing")

    tick(0.60, "Finding pieces")
    pieces = make_pieces(cells)
    # Pre-hollowing volume per cell, shared among that cell's pieces by printed volume.
    cell_printed: dict[tuple, float] = {}
    for p in pieces:
        cell_printed[p.cell] = cell_printed.get(p.cell, 0.0) + p.solid.volume()
    piece_solid = {id(p): solid_volume[p.cell] * p.solid.volume() / cell_printed[p.cell] for p in pieces}
    before = {id(p): p.solid.volume() for p in pieces}

    if opt.merge_small:
        merges = merge_small_pieces(pieces, plan, opt.bed, opt.margin)
        if merges:
            log(f"Merged {merges} small fragment(s) into neighbours")
    assign_ids(pieces)
    joints = find_joints(pieces, plan)
    link_neighbors(pieces, joints)
    log(f"{len(pieces)} pieces, {len(joints)} joints")

    # A merged piece's solid-equivalent scales with its grown printed volume.
    piece_solid_volume = {p.id: piece_solid[id(p)] * p.solid.volume() / before[id(p)] for p in pieces}

    warnings: list[str] = []
    holes: dict[str, list[Manifold]] = {p.id: [] for p in pieces}
    if opt.dowels and joints:
        holes, w = place_dowels(
            pieces, joints, opt.dowel,
            tick=lambda i, n: tick(0.65 + 0.2 * i / n, f"Placing dowels: joint {i + 1} of {n}"),
        )
        warnings += w
        log(f"{sum(len(j.dowels) for j in joints)} dowels placed")

    tick(0.85, "Choosing print orientations")
    placements = {p.id: choose_placement(p, joints, opt.bed) for p in pieces}

    labels: dict[str, Manifold] = {}
    if opt.labels and joints:
        tick(0.89, "Labelling pieces")
        bed_faces = {pid: pl.bed_face for pid, pl in placements.items()}
        labels, w = label_cutters(pieces, joints, opt.dowel, bed_faces)
        warnings += w

    finished: list[FinishedPiece] = []
    touching: list[str] = []
    for i, p in enumerate(pieces):
        tick(0.92 + 0.08 * i / len(pieces), f"Finishing piece {p.id}")
        cut = holes.get(p.id, [])
        if p.id in labels:
            cut = cut + [labels[p.id]]
        final = p.solid
        if cut:
            final = p.solid - Manifold.batch_boolean(cut, OpType.Add)
        final = clean(final)
        if not survives_stl(final):
            touching.append(p.id)
        fill = plan_fill(
            final, purpose=opt.purpose, nozzle=opt.nozzle, layer_height=opt.layer_height, density=opt.density,
            hollow_wall=opt.wall if p.hollow else None, solid_volume=piece_solid_volume[p.id],
        )
        finished.append(FinishedPiece(p, final, placements[p.id], fill))
        pl = placements[p.id]
        if any(s > b + 1e-6 for s, b in zip(pl.size, opt.bed)):
            warnings.append(f"{p.id}: {pl.size[0]:.0f}x{pl.size[1]:.0f}x{pl.size[2]:.0f} mm does not fit the bed")

    if touching:
        hint = " Smoothing can push surfaces that were close together into contact; a larger --smooth-edge or no --smooth may avoid it." if opt.smooth else ""
        warnings.append(
            f"{', '.join(touching)}: surfaces touch along a few edges. Slicers repair this on import, but check these pieces.{hint}"
        )

    pins, pin_counts = pin_plate(joints, opt.dowel) if opt.dowels else (None, {})
    tick(1.0, "Done")
    return Result(opt, str(path), factor, size, plan, finished, joints, pins, pin_counts, warnings, dropped)

# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Michael Christopher Stone
"""Cut planning and splitting a model into bed-sized pieces.

Cuts are axis-aligned planes forming a (possibly uneven) grid. For each axis we
use the minimum number of cuts that keeps every cell within the bed size, then
slide the cuts with dynamic programming to minimise the total cut area —
which puts seams at narrow points (necks, wrists, gaps) and keeps glue joints
small and inconspicuous.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from functools import reduce

import numpy as np
from manifold3d import CrossSection, Manifold
from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry

AXES = "xyz"
# Plane frame for a cut normal to axis k: (u, v) axes, cyclic so u x v = +k.
PLANE_UV = {0: (1, 2), 1: (2, 0), 2: (0, 1)}

SECTION_EPS = 0.01  # mm offset used when slicing just inside a cut face
MIN_PIECE_VOLUME = 1.0  # mm^3; smaller fragments are numerical debris


def to_plane_frame(m: Manifold, axis: int) -> Manifold:
    """Rotate so `axis` becomes Z and its plane frame (u, v) becomes (X, Y)."""
    if axis == 2:
        return m
    u, v = PLANE_UV[axis]
    mat = np.zeros((3, 4))
    mat[0, u] = mat[1, v] = mat[2, axis] = 1.0
    return m.transform(mat)


def section(m: Manifold, axis: int, pos: float) -> CrossSection:
    return to_plane_frame(m, axis).slice(pos)


def cross_section_to_shapely(cs: CrossSection) -> BaseGeometry:
    polys = [Polygon(c) for c in cs.to_polygons() if len(c) >= 3]
    polys = [p.buffer(0) for p in polys if p.area > 0]
    if not polys:
        return Polygon()
    # Even-odd composition handles holes and islands-in-holes.
    return reduce(lambda a, b: a.symmetric_difference(b), polys)


@dataclass
class CutPlan:
    cuts: dict[int, list[float]]  # axis -> sorted cut positions
    bounds: tuple[float, ...]

    def describe(self) -> str:
        parts = []
        for axis in range(3):
            pos = self.cuts.get(axis, [])
            if pos:
                parts.append(f"{AXES[axis]}=" + ",".join(f"{p:.1f}" for p in pos))
        return " ".join(parts) or "(no cuts needed)"


@dataclass
class Cell:
    index: tuple[int, int, int]
    bounds: tuple[float, ...]  # x0, y0, z0, x1, y1, z1
    solid: Manifold
    hollow: bool = False


@dataclass
class Piece:
    id: str
    solid: Manifold  # in model (assembled) coordinates
    cell: tuple[int, int, int]
    hollow: bool = False
    neighbors: list[str] = field(default_factory=list)


@dataclass
class Joint:
    """Contact region between two pieces on a cut plane.

    `neg` is the piece on the -axis side, `pos` on the +axis side. `region` is
    in the plane's (u, v) frame (see PLANE_UV).
    """

    neg: str
    pos: str
    axis: int
    coord: float
    region: BaseGeometry
    dowels: list[tuple[float, float]] = field(default_factory=list)
    dowel_depths: list[float] = field(default_factory=list)


def _area_profile(m: Manifold, axis: int, positions: np.ndarray) -> np.ndarray:
    framed = to_plane_frame(m, axis)
    return np.array([framed.slice(float(p)).area() for p in positions])


def _optimise_axis(
    m: Manifold, axis: int, lo: float, hi: float, max_cell: float, optimise: bool
) -> list[float]:
    extent = hi - lo
    n_cuts = max(0, math.ceil(extent / max_cell - 1e-9) - 1)
    if n_cuts == 0:
        return []
    even = [lo + extent * (i + 1) / (n_cuts + 1) for i in range(n_cuts)]
    if not optimise:
        return even

    step = max(1.0, extent / 600)
    cand = np.arange(lo + step, hi - step / 2, step)
    cost = _area_profile(m, axis, cand)
    # Light preference for even spacing breaks ties in empty regions.
    spacing = extent / (n_cuts + 1)
    min_cell = min(0.25 * max_cell, 0.9 * spacing)

    inf = float("inf")
    f = np.full((n_cuts, len(cand)), inf)
    back = np.full((n_cuts, len(cand)), -1, dtype=int)
    for j, c in enumerate(cand):
        if min_cell <= c - lo <= max_cell:
            f[0, j] = cost[j]
    for i in range(1, n_cuts):
        for j, c in enumerate(cand):
            d = c - cand
            ok = (d >= min_cell) & (d <= max_cell) & np.isfinite(f[i - 1])
            if ok.any():
                k = int(np.argmin(np.where(ok, f[i - 1], inf)))
                f[i, j] = f[i - 1, k] + cost[j]
                back[i, j] = k
    tail = (hi - cand >= min_cell) & (hi - cand <= max_cell)
    last = np.where(tail, f[-1], inf)
    if not np.isfinite(last).any():
        return even
    j = int(np.argmin(last))
    out = []
    for i in range(n_cuts - 1, -1, -1):
        out.append(float(cand[j]))
        j = back[i, j]
    return sorted(out)


def plan_cuts(
    m: Manifold,
    bed: tuple[float, float, float],
    *,
    margin: float = 5.0,
    optimise: bool = True,
    manual: dict[int, list[float]] | None = None,
) -> CutPlan:
    bounds = m.bounding_box()
    cuts: dict[int, list[float]] = {}
    for axis in range(3):
        lo, hi = bounds[axis], bounds[axis + 3]
        if manual and axis in manual:
            cuts[axis] = sorted(p for p in manual[axis] if lo < p < hi)
            continue
        max_cell = bed[axis] - margin
        if max_cell <= 0:
            raise ValueError("Bed size must exceed the margin")
        cuts[axis] = _optimise_axis(m, axis, lo, hi, max_cell, optimise)
    return CutPlan(cuts=cuts, bounds=tuple(bounds))


def cut_cells(m: Manifold, plan: CutPlan) -> list[Cell]:
    """Split the model along the plan's grid; empty cells are dropped."""
    cells: list[tuple[Manifold, tuple, list[float]]] = [(m, (), [])]
    for axis in range(3):
        cuts = plan.cuts.get(axis, [])
        edges = [plan.bounds[axis]] + cuts + [plan.bounds[axis + 3]]
        normal = [0.0, 0.0, 0.0]
        normal[axis] = 1.0
        nxt = []
        for solid, idx, span in cells:
            rest = solid
            for i, c in enumerate(cuts):
                above, below = rest.split_by_plane(normal, c)
                nxt.append((below, idx + (i,), span + [(edges[i], edges[i + 1])]))
                rest = above
            nxt.append((rest, idx + (len(cuts),), span + [(edges[-2], edges[-1])]))
        cells = nxt
    out = []
    for solid, idx, span in cells:
        if solid.is_empty() or solid.volume() < MIN_PIECE_VOLUME:
            continue
        bounds = tuple(s[0] for s in span) + tuple(s[1] for s in span)
        out.append(Cell(idx, bounds, solid))
    return out


def make_pieces(cells: list[Cell]) -> list[Piece]:
    """Connected components of each cell become pieces (IDs assigned later)."""
    pieces: list[Piece] = []
    for cell in cells:
        for comp in cell.solid.decompose():
            if comp.volume() >= MIN_PIECE_VOLUME:
                pieces.append(Piece(id=f"tmp{len(pieces)}", solid=comp, cell=cell.index, hollow=cell.hollow))
    return pieces


def assign_ids(pieces: list[Piece]) -> None:
    """Letter = layer from the bottom (Z), number = position within the layer."""
    pieces.sort(key=lambda p: (p.cell[2], p.cell[1], p.cell[0], p.solid.bounding_box()[0]))
    counters: dict[int, int] = {}
    for p in pieces:
        layer = p.cell[2]
        counters[layer] = counters.get(layer, 0) + 1
        letter = chr(ord("A") + layer) if layer < 26 else f"L{layer}-"
        p.id = f"{letter}{counters[layer]}"


def _fits_any_orientation(m: Manifold, bed: tuple[float, float, float], margin: float) -> bool:
    b = m.bounding_box()
    dims = sorted(b[k + 3] - b[k] for k in range(3))
    return all(d <= s - margin for d, s in zip(dims, sorted(bed)))


def merge_small_pieces(
    pieces: list[Piece], plan: CutPlan, bed: tuple[float, float, float], margin: float = 5.0
) -> int:
    """Fuse small fragments into the neighbour they share most contact with,
    when the result still fits the bed. Returns the number of merges."""
    if not pieces:
        return 0
    largest = max(p.solid.volume() for p in pieces)
    small_dim = 0.25 * min(bed)
    merges = 0
    while True:
        joints = find_joints(pieces, plan)
        by_id = {p.id: p for p in pieces}

        def is_small(p: Piece) -> bool:
            b = p.solid.bounding_box()
            return max(b[k + 3] - b[k] for k in range(3)) < small_dim or p.solid.volume() < 0.05 * largest

        merged = False
        for p in sorted((p for p in pieces if is_small(p)), key=lambda p: p.solid.volume()):
            contacts: dict[str, float] = {}
            for j in joints:
                if p.id in (j.neg, j.pos):
                    other = j.pos if j.neg == p.id else j.neg
                    contacts[other] = contacts.get(other, 0.0) + j.region.area
            for other_id, _ in sorted(contacts.items(), key=lambda kv: -kv[1]):
                other = by_id[other_id]
                fused = p.solid + other.solid
                if _fits_any_orientation(fused, bed, margin):
                    keep = other if other.solid.volume() >= p.solid.volume() else p
                    keep.solid, keep.hollow = fused, p.hollow or other.hollow
                    pieces.remove(p if keep is other else other)
                    merges += 1
                    merged = True
                    break
            if merged:
                break
        if not merged:
            return merges


def link_neighbors(pieces: list[Piece], joints: list["Joint"]) -> None:
    by_id = {p.id: p for p in pieces}
    for j in joints:
        by_id[j.neg].neighbors.append(j.pos)
        by_id[j.pos].neighbors.append(j.neg)


def split_model(m: Manifold, plan: CutPlan) -> tuple[list[Piece], list["Joint"]]:
    """Solid split without hollowing (convenience for tests and simple use)."""
    pieces = make_pieces(cut_cells(m, plan))
    assign_ids(pieces)
    joints = find_joints(pieces, plan)
    link_neighbors(pieces, joints)
    return pieces, joints


def find_joints(pieces: list[Piece], plan: CutPlan, min_area: float = 4.0) -> list[Joint]:
    joints: list[Joint] = []
    tol = 1e-3
    for axis in range(3):
        for c in plan.cuts.get(axis, []):
            # Merged pieces may straddle a plane, so test for material on each
            # side rather than requiring a bounding-box face on the plane.
            boxes = {p.id: p.solid.bounding_box() for p in pieces}
            below = [p for p in pieces if boxes[p.id][axis] < c - tol and boxes[p.id][axis + 3] > c - tol]
            above = [p for p in pieces if boxes[p.id][axis + 3] > c + tol and boxes[p.id][axis] < c + tol]
            if not below or not above:
                continue
            sec_below = {p.id: section(p.solid, axis, c - SECTION_EPS) for p in below}
            sec_above = {p.id: section(p.solid, axis, c + SECTION_EPS) for p in above}
            for a in below:
                for b in above:
                    if a is b or not _bbox_overlap(a.solid, b.solid, axis):
                        continue
                    contact = sec_below[a.id] ^ sec_above[b.id]
                    if contact.is_empty():
                        continue
                    if contact.area() < min_area:
                        continue
                    region = cross_section_to_shapely(contact)
                    if region.is_empty:
                        continue
                    joints.append(Joint(neg=a.id, pos=b.id, axis=axis, coord=c, region=region))
    return joints


def _bbox_overlap(a: Manifold, b: Manifold, skip_axis: int) -> bool:
    ba, bb = a.bounding_box(), b.bounding_box()
    for k in range(3):
        if k == skip_axis:
            continue
        if ba[k + 3] < bb[k] or bb[k + 3] < ba[k]:
            return False
    return True


def region_parts(region: BaseGeometry) -> list[Polygon]:
    if isinstance(region, Polygon):
        return [region] if not region.is_empty else []
    if isinstance(region, MultiPolygon):
        return list(region.geoms)
    return [g for g in getattr(region, "geoms", []) if isinstance(g, Polygon)]

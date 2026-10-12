# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Michael Christopher Stone
"""Dowel-pin connectors placed on the contact faces between pieces."""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import shapely
from manifold3d import Manifold, OpType
from shapely.ops import polylabel
from shapely.geometry import Point

from .split import PLANE_UV, SECTION_EPS, Joint, Piece, cross_section_to_shapely, region_parts, section

SEGMENTS = 40


@dataclass
class DowelSpec:
    diameter: float = 6.0  # pin diameter, mm
    clearance: float = 0.4  # hole diameter minus pin diameter
    depth: float = 15.0  # nominal insertion depth per side
    min_wall: float = 2.5  # material kept between hole and part surface
    area_per_dowel: float = 2500.0  # contact area (mm^2) that earns one more dowel
    max_per_joint: int = 4
    chamfer: float = 0.6

    @property
    def hole_r(self) -> float:
        return (self.diameter + self.clearance) / 2


def from_plane_frame(m: Manifold, axis: int) -> Manifold:
    """Inverse of split.to_plane_frame: plane (u, v, w) -> model (x, y, z)."""
    if axis == 2:
        return m
    u, v = PLANE_UV[axis]
    mat = np.zeros((3, 4))
    mat[u, 0] = mat[v, 1] = mat[axis, 2] = 1.0
    return m.transform(mat)


def _spread_points(geom, n: int, spacing: float) -> list[tuple[float, float]]:
    """Pick n well-separated points inside geom (k-means over candidate points)."""
    parts = region_parts(geom)
    largest = max(parts, key=lambda g: g.area)
    if n == 1:
        p = polylabel(largest, tolerance=0.5)
        return [(p.x, p.y)]
    x0, y0, x1, y1 = geom.bounds
    step = max(spacing, math.sqrt(geom.area / 3000))
    xs, ys = np.meshgrid(np.arange(x0, x1 + step, step), np.arange(y0, y1 + step, step))
    cand = np.column_stack([xs.ravel(), ys.ravel()])
    cand = cand[shapely.contains_xy(geom, cand[:, 0], cand[:, 1])]
    # Hollow shells give thin ring-shaped regions the grid can miss; sample their edges too.
    edge_pts = []
    for poly in parts:
        for ring in [poly.exterior, *poly.interiors]:
            k = max(4, int(ring.length / spacing))
            edge_pts += [ring.interpolate(i / k, normalized=True).coords[0] for i in range(k)]
    if edge_pts:
        cand = np.vstack([cand, np.array(edge_pts)])
    if len(cand) < n:
        p = polylabel(largest, tolerance=0.5)
        return [(p.x, p.y)]
    # Deterministic farthest-point seeding, then Lloyd iterations.
    centers = [cand[np.argmax(np.linalg.norm(cand - cand.mean(0), axis=1))]]
    for _ in range(n - 1):
        d = np.min([np.linalg.norm(cand - c, axis=1) for c in centers], axis=0)
        centers.append(cand[np.argmax(d)])
    centers = np.array(centers)
    for _ in range(25):
        label = np.argmin(np.linalg.norm(cand[:, None] - centers[None], axis=2), axis=1)
        centers = np.array([cand[label == k].mean(0) if (label == k).any() else centers[k] for k in range(n)])
    # Snap to actual candidates (centroid of a non-convex cluster may lie outside).
    return [tuple(cand[np.argmin(np.linalg.norm(cand - c, axis=1))]) for c in centers]


def _material_through(piece: Manifold, axis: int, coord: float, side: int, depth: float):
    """Region of the cut plane under which the piece is solid for `depth` mm.

    Intersects cross-sections sampled every ~1.5 mm going into the piece
    (side=-1 for the piece below the plane, +1 above). This accounts for shells
    that cross the plane at a slant, where a hole would break out partway down.
    """
    steps = np.unique(np.append(np.arange(SECTION_EPS, depth, 1.5), depth))
    region = None
    for s in steps:
        sec = cross_section_to_shapely(section(piece, axis, coord + side * s))
        region = sec if region is None else region.intersection(sec)
        if region.is_empty:
            break
    return region


def _hole_cutter(spec: DowelSpec, depth: float) -> Manifold:
    """Through-plane hole centred on w=0 extending `depth`+0.5 each way, chamfered at the mouth."""
    r = spec.hole_r
    h = depth + 0.5
    body = Manifold.cylinder(2 * h, r, r, SEGMENTS).translate([0, 0, -h])
    c = spec.chamfer
    up = Manifold.cylinder(c, r + c, r, SEGMENTS)
    down = Manifold.cylinder(c, r, r + c, SEGMENTS).translate([0, 0, -c])
    return Manifold.batch_boolean([body, up, down], OpType.Add)


def _fits(piece: Manifold, cutter: Manifold) -> bool:
    inside = (piece ^ cutter).volume()
    return inside >= 0.97 * cutter.volume()


def place_dowels(
    pieces: list[Piece], joints: list[Joint], spec: DowelSpec, tick=None
) -> tuple[dict[str, list[Manifold]], list[str]]:
    """Choose dowel positions per joint and return hole cutters per piece id.

    Joints are updated in place with their dowel coordinates. Returns
    (cutters_by_piece, warnings).
    """
    by_id = {p.id: p for p in pieces}
    cutters: dict[str, list[Manifold]] = {p.id: [] for p in pieces}
    warnings: list[str] = []
    margin = spec.hole_r + spec.min_wall

    for i, j in enumerate(joints):
        if tick:
            tick(i, len(joints))
        a, b = by_id[j.neg].solid, by_id[j.pos].solid
        wanted = max(1, min(spec.max_per_joint, round(j.region.area / spec.area_per_dowel)))
        if j.region.buffer(-margin).is_empty:
            warnings.append(f"{j.neg}/{j.pos}: contact ({j.region.area:.0f} mm^2) too small for a dowel - glue only")
            continue
        for depth in (spec.depth, spec.depth * 0.6):
            reach = depth + 0.5  # hole is a little deeper than the pin
            feasible = j.region
            for piece, side in ((a, -1), (b, +1)):
                feasible = feasible.intersection(_material_through(piece, j.axis, j.coord, side, reach))
                if feasible.is_empty:
                    break
            feasible = feasible.buffer(-margin) if not feasible.is_empty else feasible
            if feasible.is_empty or not region_parts(feasible):
                continue
            pts = _spread_points(feasible, wanted, spacing=spec.diameter / 2)
            pts = _enforce_spacing(pts, 3 * spec.diameter)
            for (u, v) in pts:
                hole = from_plane_frame(_hole_cutter(spec, depth).translate([u, v, j.coord]), j.axis)
                neg_half = hole.trim_by_plane(_normal(j.axis, -1), -j.coord)
                pos_half = hole.trim_by_plane(_normal(j.axis, 1), j.coord)
                if _fits(a, neg_half) and _fits(b, pos_half):
                    cutters[j.neg].append(hole)
                    cutters[j.pos].append(hole)
                    j.dowels.append((u, v))
                    j.dowel_depths.append(depth)
            if j.dowels:
                break
        if not j.dowels:
            warnings.append(f"{j.neg}/{j.pos}: material too thin for a {spec.diameter:g} mm dowel - glue only")
    return cutters, warnings


def _normal(axis: int, sign: int) -> list[float]:
    n = [0.0, 0.0, 0.0]
    n[axis] = float(sign)
    return n


def _enforce_spacing(pts: list[tuple[float, float]], min_d: float) -> list[tuple[float, float]]:
    kept: list[tuple[float, float]] = []
    for p in pts:
        if all(Point(p).distance(Point(q)) >= min_d for q in kept):
            kept.append(p)
    return kept


def make_pin(spec: DowelSpec, depth: float) -> Manifold:
    """A printable pin lying on its side: flattened bottom, chamfered ends.

    Length leaves 1 mm total slack in the holes (each hole is depth + 0.5).
    """
    r = spec.diameter / 2
    length = 2 * depth
    c = min(spec.chamfer, r / 3)
    body = Manifold.cylinder(length - 2 * c, r, r, SEGMENTS).translate([0, 0, c])
    end0 = Manifold.cylinder(c, r - c, r, SEGMENTS)
    end1 = Manifold.cylinder(c, r, r - c, SEGMENTS).translate([0, 0, length - c])
    pin = Manifold.batch_boolean([body, end0, end1], OpType.Add)
    pin = pin.rotate([0, 90, 0])  # lie along X
    flat = r * 0.15
    pin = pin.trim_by_plane([0, 0, 1], -r + flat)
    x0, y0, z0, *_ = pin.bounding_box()
    return pin.translate([-x0, -y0, -z0])


def pin_plate(joints: list[Joint], spec: DowelSpec, gap: float = 4.0) -> tuple[Manifold | None, dict[float, int]]:
    """All pins for the project laid out in rows, plus a count per pin length."""
    counts: dict[float, int] = {}
    for j in joints:
        for d in j.dowel_depths:
            counts[d] = counts.get(d, 0) + 1
    if not counts:
        return None, counts
    pins: list[Manifold] = []
    y = 0.0
    for depth, n in sorted(counts.items()):
        pin = make_pin(spec, depth)
        _, _, _, px, py, _ = pin.bounding_box()
        per_row = max(1, int(200 // (px + gap)))
        for i in range(n):
            row, col = divmod(i, per_row)
            pins.append(pin.translate([col * (px + gap), y + row * (py + gap), 0]))
        y += math.ceil(n / per_row) * (py + gap)
    return Manifold.batch_boolean(pins, OpType.Add), counts

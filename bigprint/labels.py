# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Michael Christopher Stone
"""Debossed piece IDs on cut faces, rendered with a built-in stroke font.

Labels go on a contact face so they are hidden once the model is assembled.
The text reads correctly when looking at the face from outside the piece.
"""

from __future__ import annotations

import math

import numpy as np
import shapely
from manifold3d import CrossSection, FillRule, Manifold
from shapely.affinity import rotate as shp_rotate
from shapely.affinity import scale as shp_scale
from shapely.affinity import translate as shp_translate
from shapely.geometry import LineString, Point, Polygon, box
from shapely.ops import unary_union

from .connectors import DowelSpec, from_plane_frame
from .split import Joint, Piece, region_parts

# Glyphs on a 4 x 6 grid; each glyph is a list of polylines.
_O = [(1, 0), (0, 1), (0, 5), (1, 6), (3, 6), (4, 5), (4, 1), (3, 0), (1, 0)]
_P = [(0, 0), (0, 6), (3, 6), (4, 5), (4, 4), (3, 3), (0, 3)]
GLYPHS: dict[str, list[list[tuple[int, int]]]] = {
    "A": [[(0, 0), (0, 4), (2, 6), (4, 4), (4, 0)], [(0, 3), (4, 3)]],
    "B": [[(0, 0), (0, 6), (3, 6), (4, 5), (4, 4), (3, 3), (0, 3)], [(3, 3), (4, 2), (4, 1), (3, 0), (0, 0)]],
    "C": [[(4, 5), (3, 6), (1, 6), (0, 5), (0, 1), (1, 0), (3, 0), (4, 1)]],
    "D": [[(0, 0), (0, 6), (2, 6), (4, 4), (4, 2), (2, 0), (0, 0)]],
    "E": [[(4, 6), (0, 6), (0, 0), (4, 0)], [(0, 3), (3, 3)]],
    "F": [[(4, 6), (0, 6), (0, 0)], [(0, 3), (3, 3)]],
    "G": [[(4, 5), (3, 6), (1, 6), (0, 5), (0, 1), (1, 0), (3, 0), (4, 1), (4, 3), (2, 3)]],
    "H": [[(0, 0), (0, 6)], [(4, 0), (4, 6)], [(0, 3), (4, 3)]],
    "I": [[(1, 6), (3, 6)], [(2, 6), (2, 0)], [(1, 0), (3, 0)]],
    "J": [[(4, 6), (4, 1), (3, 0), (1, 0), (0, 1)]],
    "K": [[(0, 0), (0, 6)], [(4, 6), (0, 2)], [(1, 3), (4, 0)]],
    "L": [[(0, 6), (0, 0), (4, 0)]],
    "M": [[(0, 0), (0, 6), (2, 3), (4, 6), (4, 0)]],
    "N": [[(0, 0), (0, 6), (4, 0), (4, 6)]],
    "O": [_O],
    "P": [_P],
    "Q": [_O, [(2, 2), (4, 0)]],
    "R": [_P, [(2, 3), (4, 0)]],
    "S": [[(4, 5), (3, 6), (1, 6), (0, 5), (0, 4), (1, 3), (3, 3), (4, 2), (4, 1), (3, 0), (1, 0), (0, 1)]],
    "T": [[(0, 6), (4, 6)], [(2, 6), (2, 0)]],
    "U": [[(0, 6), (0, 1), (1, 0), (3, 0), (4, 1), (4, 6)]],
    "V": [[(0, 6), (2, 0), (4, 6)]],
    "W": [[(0, 6), (1, 0), (2, 3), (3, 0), (4, 6)]],
    "X": [[(0, 0), (4, 6)], [(0, 6), (4, 0)]],
    "Y": [[(0, 6), (2, 3), (4, 6)], [(2, 3), (2, 0)]],
    "Z": [[(0, 6), (4, 6), (0, 0), (4, 0)]],
    "0": [_O, [(0, 1), (4, 5)]],
    "1": [[(1, 5), (2, 6), (2, 0)], [(1, 0), (3, 0)]],
    "2": [[(0, 5), (1, 6), (3, 6), (4, 5), (4, 4), (0, 0), (4, 0)]],
    "3": [[(0, 5), (1, 6), (3, 6), (4, 5), (4, 4), (3, 3), (4, 2), (4, 1), (3, 0), (1, 0), (0, 1)], [(1, 3), (3, 3)]],
    "4": [[(3, 0), (3, 6), (0, 2), (4, 2)]],
    "5": [[(4, 6), (0, 6), (0, 3), (3, 3), (4, 2), (4, 1), (3, 0), (0, 0)]],
    "6": [[(4, 5), (3, 6), (1, 6), (0, 5), (0, 1), (1, 0), (3, 0), (4, 1), (4, 2), (3, 3), (0, 3)]],
    "7": [[(0, 6), (4, 6), (1, 0)]],
    "8": [[(1, 3), (0, 4), (0, 5), (1, 6), (3, 6), (4, 5), (4, 4), (3, 3), (1, 3), (0, 2), (0, 1), (1, 0), (3, 0), (4, 1), (4, 2), (3, 3)]],
    "9": [[(0, 1), (1, 0), (3, 0), (4, 1), (4, 5), (3, 6), (1, 6), (0, 5), (0, 4), (1, 3), (4, 3)]],
    "-": [[(1, 3), (3, 3)]],
}

LABEL_DEPTH = 0.8
MIN_STROKE = 0.9  # mm; two 0.45 mm extrusion lines


def text_outline(text: str, height: float) -> Polygon:
    """2D outline of `text` with baseline at y=0, starting at x=0."""
    unit = height / 6
    stroke = max(MIN_STROKE, height * 0.13)
    shapes = []
    for i, ch in enumerate(text.upper()):
        for line in GLYPHS.get(ch, []):
            pts = [((x + 6 * i) * unit, y * unit) for x, y in line]
            shapes.append(LineString(pts).buffer(stroke / 2, quad_segs=4))
    return unary_union(shapes)


def _to_cross_section(geom) -> CrossSection:
    contours = []
    for poly in region_parts(geom):
        contours.append(np.asarray(poly.exterior.coords)[:-1])
        contours.extend(np.asarray(r.coords)[:-1] for r in poly.interiors)
    return CrossSection(contours, FillRule.EvenOdd)


def _find_spot(free, w: float, h: float):
    """(x, y, angle) where a w x h box fits inside `free`, or None.

    Tries the most interior points first, at 0° and aligned with the nearest
    edge, so labels can follow the curve of a hollow piece's collar ring.
    """
    x0, y0, x1, y1 = free.bounds
    step = max(0.75, min(w, h) / 4)
    xs, ys = np.meshgrid(np.arange(x0, x1, step), np.arange(y0, y1, step))
    cand = np.column_stack([xs.ravel(), ys.ravel()])
    if len(cand) == 0:
        return None
    cand = cand[shapely.contains_xy(free, cand[:, 0], cand[:, 1])]
    if len(cand) == 0:
        return None
    boundary = free.boundary
    depth = shapely.distance(shapely.points(cand), boundary)
    order = np.argsort(-depth)
    cand, depth = cand[order], depth[order]
    keep = depth >= min(w, h) / 2 - 1e-6  # a box can't fit anywhere shallower
    cand = cand[keep][:400]
    shapely.prepare(free)
    base = box(-w / 2, -h / 2, w / 2, h / 2)
    for cx, cy in cand:
        pt = Point(cx, cy)
        near = boundary.interpolate(boundary.project(pt))
        ahead = boundary.interpolate(boundary.project(pt) + 1.0)
        tangent = math.degrees(math.atan2(ahead.y - near.y, ahead.x - near.x))
        tangent = (tangent + 90) % 180 - 90  # keep text roughly upright
        for angle in (0.0, tangent):
            fp = shp_translate(shp_rotate(base, angle, origin=(0, 0)), cx, cy)
            if free.contains(fp):
                return cx, cy, angle
    return None


def label_cutters(
    pieces: list[Piece],
    joints: list[Joint],
    spec: DowelSpec,
    bed_faces: dict[str, tuple[int, int] | None] | None = None,
    max_height: float = 12.0,
    min_height: float = 5.0,
) -> tuple[dict[str, Manifold], list[str]]:
    """Return a deboss cutter per piece id (model frame), plus warnings.

    Faces that will sit on the print bed are used only as a last resort, since
    first-layer squish blurs debossed text.
    """
    bed_faces = bed_faces or {}
    cutters: dict[str, Manifold] = {}
    warnings: list[str] = []
    keepout = spec.hole_r + spec.chamfer + 1.5

    faces: dict[str, list[tuple[Joint, int]]] = {p.id: [] for p in pieces}
    for j in joints:
        faces[j.neg].append((j, +1))  # outward normal +axis
        faces[j.pos].append((j, -1))

    for piece in pieces:
        bed = bed_faces.get(piece.id)
        options = sorted(
            faces[piece.id], key=lambda f: ((f[0].axis, f[1]) == bed, -f[0].region.area)
        )
        if not options:
            continue
        placed = False
        for j, sign in options:
            free = j.region.buffer(-1.5)
            for (u, v) in j.dowels:
                free = free.difference(Point(u, v).buffer(keepout))
            if free.is_empty:
                continue
            h = max_height
            while h >= min_height and not placed:
                text = text_outline(piece.id, h)
                tx0, ty0, tx1, ty1 = text.bounds
                spot = _find_spot(free, tx1 - tx0 + 2, ty1 - ty0 + 2)
                if spot is not None:
                    text = shp_translate(text, -(tx0 + tx1) / 2, -(ty0 + ty1) / 2)
                    if sign < 0:
                        # Viewed from -w, mirror so it reads correctly.
                        text = shp_scale(text, xfact=-1, yfact=1, origin=(0, 0))
                    text = shp_rotate(text, spot[2], origin=(0, 0))
                    text = shp_translate(text, spot[0], spot[1])
                    cs = _to_cross_section(text)
                    eps = 0.05
                    solid = cs.extrude(LABEL_DEPTH + eps)
                    if sign > 0:  # material lies below the plane
                        solid = solid.translate([0, 0, j.coord - LABEL_DEPTH])
                    else:
                        solid = solid.translate([0, 0, j.coord - eps])
                    cutters[piece.id] = from_plane_frame(solid, j.axis)
                    placed = True
                h *= 0.8
            if placed:
                break
        if not placed:
            warnings.append(f"{piece.id}: no room on cut faces for a label")
    return cutters, warnings

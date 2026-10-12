# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Michael Christopher Stone
"""Choose a print orientation per piece by minimising unsupported overhang.

Candidates are the six axis-aligned "face down" orientations (each with an
optional 90° spin to fit the bed). Flat cut faces usually win, which needs no
supports, but for a hollow shell this also avoids turning the model's own
flat bottom into a bridged ceiling.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from manifold3d import Manifold

from .split import Joint, Piece

# Rotation (Euler degrees, as Manifold.rotate) that turns an outward normal
# along (axis, sign) into -Z.
_FACE_DOWN = {
    (2, -1): (0, 0, 0),
    (2, +1): (180, 0, 0),
    (0, +1): (0, 90, 0),
    (0, -1): (0, -90, 0),
    (1, +1): (-90, 0, 0),
    (1, -1): (90, 0, 0),
}

OVERHANG_DEG = 45.0


@dataclass
class Placement:
    rotation: tuple[float, float, float]
    spin: float  # extra rotation about Z, degrees
    bed_face: tuple[int, int] | None  # (axis, sign) of the face on the bed
    size: tuple[float, float, float]
    overhang: float = 0.0  # mm^2 needing support

    def apply(self, m: Manifold) -> Manifold:
        m = m.rotate(list(self.rotation))
        if self.spin:
            m = m.rotate([0, 0, self.spin])
        x0, y0, z0, x1, y1, _ = m.bounding_box()
        return m.translate([-(x0 + x1) / 2, -(y0 + y1) / 2, -z0])


def _size(m: Manifold) -> tuple[float, float, float]:
    b = m.bounding_box()
    return b[3] - b[0], b[4] - b[1], b[5] - b[2]


def _fits(size, bed) -> bool:
    return size[0] <= bed[0] and size[1] <= bed[1] and size[2] <= bed[2]


def _overhang_and_contact(m: Manifold) -> tuple[float, float]:
    mesh = m.to_mesh()
    v = np.asarray(mesh.vert_properties)[:, :3]
    t = np.asarray(mesh.tri_verts)
    a, b, c = v[t[:, 0]], v[t[:, 1]], v[t[:, 2]]
    cross = np.cross(b - a, c - a)
    area = np.linalg.norm(cross, axis=1) / 2
    nz = cross[:, 2] / np.maximum(2 * area, 1e-12)
    zmin = v[:, 2].min()
    on_bed = (np.maximum(np.maximum(a[:, 2], b[:, 2]), c[:, 2]) - zmin) < 0.05
    down = nz < -math.cos(math.radians(90 - OVERHANG_DEG))
    return float(area[down & ~on_bed].sum()), float(area[on_bed & (nz < -0.99)].sum())


def choose_placement(piece: Piece, joints: list[Joint], bed: tuple[float, float, float]) -> Placement:
    cut_faces = {(j.axis, +1) for j in joints if j.neg == piece.id}
    cut_faces |= {(j.axis, -1) for j in joints if j.pos == piece.id}

    best: tuple[float, Placement] | None = None
    for key, rot in _FACE_DOWN.items():
        rotated = piece.solid.rotate(list(rot))
        for spin in (0.0, 90.0):
            s = _size(rotated.rotate([0, 0, spin]) if spin else rotated)
            if _fits(s, bed):
                break
        else:
            continue
        overhang, contact = _overhang_and_contact(rotated)
        # Support area dominates; prefer bigger bed contact and real cut faces to break ties.
        score = overhang - 0.05 * contact - (1.0 if key in cut_faces else 0.0)
        pl = Placement(rot, spin, key if key in cut_faces else None, s, overhang)
        if best is None or score < best[0]:
            best = (score, pl)
    if best is not None:
        return best[1]
    s = _size(piece.solid)
    return Placement((0, 0, 0), 0.0, None, s, float("nan"))

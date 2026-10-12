# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Michael Christopher Stone
"""Shell hollowing with open cut faces and thickened joint collars.

Each cell's cavity is computed from the distance to the *original* model
surface, not the cell's cut faces, so the cavity runs straight through the
cut planes. Every joint face becomes a ring, with no closed ceilings to bridge.
Cells that lie wholly inside the hollow vanish and need no printing.

Near each cut plane the wall thickens into a collar: a straight prism normal
to the plane, measured inward from the part of the cross-section that stays
inside the model throughout the band. Dowels are straight, so a straight
collar holds them even where the surface crosses the plane at a slant. Past
the band the collar thins back to the normal wall over a 45° ramp so the
transition prints without supports.

Distances come from exact Euclidean distance transforms on a voxel grid
aligned to a global lattice, so mating pieces get identical rings.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import shapely
import trimesh
from manifold3d import Manifold
from scipy.ndimage import distance_transform_edt
from skimage.measure import marching_cubes

from .io import trimesh_to_manifold
from .split import PLANE_UV, Cell, CutPlan, cross_section_to_shapely, section


@dataclass
class HollowSpec:
    wall: float = 4.0  # shell thickness away from joints
    collar: float = 14.4  # collar width at joint faces, in the cut plane
    collar_depth: float = 17.5  # full-width band on each side of a cut plane
    voxel: float = 1.5  # grid resolution

    @classmethod
    def for_dowels(cls, wall: float, dowel, voxel: float | None = None):
        """Size the collar so a dowel hole fits with `min_wall` each side, plus
        3 mm slack for voxel error, and deep enough to hold the whole hole."""
        collar = max(wall, 2 * (dowel.hole_r + dowel.min_wall) + 3.0)
        return cls(wall=wall, collar=collar, collar_depth=dowel.depth + 2.5, voxel=voxel or min(1.5, wall / 2.5))


def _inside_mask(local: Manifold, xs: np.ndarray, ys: np.ndarray, zs: np.ndarray) -> np.ndarray:
    gx, gy = np.meshgrid(xs, ys, indexing="ij")
    mask = np.zeros((len(xs), len(ys), len(zs)), dtype=bool)
    for k, z in enumerate(zs):
        cs = local.slice(float(z))
        if cs.is_empty():
            continue
        geom = cross_section_to_shapely(cs)
        if geom.is_empty:
            continue
        shapely.prepare(geom)
        mask[:, :, k] = shapely.contains_xy(geom, gx, gy)
    return mask


def _band_core(local: Manifold, axis: int, coord: float, band: float):
    """Cross-section region that is inside the model at every depth within the band."""
    core = None
    for s in np.append(np.arange(-band, band, 1.5), band):
        sec = cross_section_to_shapely(section(local, axis, coord + s))
        core = sec if core is None else core.intersection(sec)
        if core.is_empty:
            break
    return core


def cell_cavity(model: Manifold, cell: Cell, plan: CutPlan, spec: HollowSpec) -> Manifold | None:
    """Cavity solid to subtract from a grid cell, or None if nothing to hollow."""
    planes = [(axis, c) for axis, cs in plan.cuts.items() for c in cs]
    if not planes:
        return None  # whole model in one piece: no open face to drain a cavity

    h = spec.voxel
    reach = max(spec.collar, spec.wall) + 3 * h  # distances beyond this never matter
    b = cell.solid.bounding_box()
    lo = [math.floor((b[k] - reach) / h) * h for k in range(3)]
    hi = [math.ceil((b[k + 3] + reach) / h) * h for k in range(3)]
    axes = [np.arange(lo[k], hi[k] + h / 2, h) for k in range(3)]
    if any(len(a) < 3 for a in axes):
        return None

    box = Manifold.cube([hi[k] - lo[k] + 2 * h for k in range(3)]).translate([lo[k] - h for k in range(3)])
    local = model ^ box
    inside = _inside_mask(local, *axes)
    if not inside.any():
        return None

    # Distance from each inside voxel centre to the surface (EDT measures to the
    # nearest outside voxel centre, ~h/2 beyond the surface).
    dist = distance_transform_edt(inside, sampling=h) - h / 2
    field = np.where(inside, dist - spec.wall, -h).astype(np.float32)

    # Collars: within the band around each plane the cavity must stay at least
    # `collar` (in-plane) inside the band's core section; past the band that
    # allowance shrinks 1:1, giving a 45° ramp.
    band = spec.collar_depth
    for axis, coord in planes:
        s_dist = np.abs(axes[axis] - coord)
        near = s_dist <= band + spec.collar
        if not near.any():
            continue
        u, v = PLANE_UV[axis]
        core = _band_core(local, axis, coord, band)
        gu, gv = np.meshgrid(axes[u], axes[v], indexing="ij")
        if core is None or core.is_empty:
            core_mask = np.zeros(gu.shape, dtype=bool)
        else:
            shapely.prepare(core)
            core_mask = shapely.contains_xy(core, gu, gv)
        d2 = (distance_transform_edt(core_mask, sampling=h) - h / 2).astype(np.float32)
        d2[~core_mask] = -h
        allowance = (spec.collar - np.clip(s_dist[near] - band, 0, None)).astype(np.float32)
        collar_field = d2[:, :, None] - allowance[None, None, :]
        # Reorder (u, v, w) dims to (x, y, z).
        collar_field = np.transpose(collar_field, np.argsort([u, v, axis]))
        idx = [slice(None)] * 3
        idx[axis] = np.nonzero(near)[0]
        field[tuple(idx)] = np.minimum(field[tuple(idx)], collar_field)

    if field.max() <= h * 0.5:
        return None  # thinner than its walls: stays solid

    padded = np.pad(field, 1, constant_values=-h)
    verts, faces, _, _ = marching_cubes(padded, level=0.0, spacing=(h, h, h))
    verts += np.array(lo) - h
    tm = trimesh.Trimesh(verts, faces, process=True)
    if tm.volume < 0:
        tm.invert()
    cavity = trimesh_to_manifold(tm)
    if cavity.is_empty():
        trimesh.repair.fix_normals(tm)
        cavity = trimesh_to_manifold(tm)
        if cavity.is_empty():
            return None
    # The cavity deliberately pokes through the cut planes (into the
    # neighbours' space) so subtracting it opens the faces cleanly, with no
    # coplanar skins left behind.
    return cavity.simplify(h * 0.2)


def hollow_cell(model: Manifold, cell: Cell, plan: CutPlan, spec: HollowSpec) -> Manifold | None:
    """Hollowed cell solid (possibly empty for interior cells), or None if unchanged."""
    cavity = cell_cavity(model, cell, plan, spec)
    if cavity is None:
        return None
    shell = cell.solid - cavity
    if cell.solid.volume() - shell.volume() < 1000:  # under 1 cm^3 removed
        return None
    return shell

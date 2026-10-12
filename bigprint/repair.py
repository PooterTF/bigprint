# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Michael Christopher Stone
"""Automatic repair of damaged meshes by voxel rebuild.

Downloaded and sculpted models often have small holes and overlapping or
stitched surfaces (edges shared by 3+ triangles). Solid geometry can't be cut
or hollowed until the surface is closed, so this rebuilds it:

1. Mark every voxel the surface passes through. Slits narrower than a voxel
   are sealed, and overlapping surfaces simply merge.
2. Flood-fill the outside from the grid border. Everything the flood can't
   reach is solid.
3. Blur the solid mask slightly and run marching cubes slab by slab, giving a
   smooth, watertight surface without holding a float copy of the whole grid.

Resolution is chosen at the *final* (scaled) size, so detail is lost only below
that size, and is coarsened if needed to stay within a voxel budget.
"""

from __future__ import annotations

import math
from typing import Callable

import numpy as np
import trimesh
from manifold3d import Manifold
from scipy.ndimage import binary_propagation, gaussian_filter
from skimage.measure import marching_cubes

from .io import MeshError, trimesh_to_manifold

VOXEL_BUDGET = 100_000_000  # ~100 MB per boolean grid; three are alive at once
TARGET_PITCH = 1.0  # mm at final size, when the budget allows
SLAB = 48  # z-layers per marching-cubes slab
HALO = 4  # extra layers each side so the blur matches across slabs
SIGMA = 0.7  # blur in voxels: removes stair-steps, keeps edges reasonably crisp
LEVEL = 0.8  # iso-level; calibrated so a sphere keeps its true radius (marking surface voxels grows ~half a voxel)


def choose_pitch(extents: np.ndarray, factor: float) -> tuple[float, float]:
    """(pitch in model units, pitch in final mm) for a model scaled by `factor`."""
    final = np.asarray(extents, dtype=float) * factor
    by_budget = (np.prod(final + 4 * TARGET_PITCH) / VOXEL_BUDGET) ** (1 / 3)
    final_pitch = max(TARGET_PITCH, by_budget)
    return final_pitch / factor, final_pitch


def _mark_surface(solid: np.ndarray, tris: np.ndarray, origin: np.ndarray, h: float) -> None:
    """Set every voxel a triangle passes through, by dense barycentric sampling."""
    step = h / 3  # sample spacing well under a voxel so no gaps open in the shell
    edges = np.linalg.norm(tris[:, [1, 2, 0]] - tris, axis=2).max(axis=1)
    subdiv = np.maximum(1, np.ceil(edges / step)).astype(int)
    shape = np.array(solid.shape)
    for k in np.unique(subdiv):
        group = tris[subdiv == k]
        i, j = np.meshgrid(np.arange(k + 1), np.arange(k + 1), indexing="ij")
        keep = (i + j) <= k
        w1, w2 = i[keep] / k, j[keep] / k
        w = np.stack([1 - w1 - w2, w1, w2], axis=1).astype(np.float32)  # (P, 3)
        per_batch = max(1, 4_000_000 // len(w))
        for s in range(0, len(group), per_batch):
            pts = np.einsum("pk,tkd->tpd", w, group[s : s + per_batch].astype(np.float32)).reshape(-1, 3)
            idx = np.floor((pts - origin) / h).astype(np.int64)
            np.clip(idx, 0, shape - 1, out=idx)
            solid[idx[:, 0], idx[:, 1], idx[:, 2]] = True


def voxel_repair(
    mesh: trimesh.Trimesh,
    pitch: float,
    progress: Callable[[float, str], None] | None = None,
) -> Manifold:
    """Rebuild `mesh` as a closed solid at voxel size `pitch` (model units)."""
    tick = progress or (lambda *_: None)
    h = float(pitch)
    lo = mesh.bounds[0] - 2 * h
    dims = np.ceil((mesh.bounds[1] + 2 * h - lo) / h).astype(int) + 1
    if np.prod(dims) > 2.5 * VOXEL_BUDGET:
        raise MeshError("Model is too large to repair at this resolution")

    tick(0.0, "Repair: marking the surface")
    solid = np.zeros(dims, dtype=bool)
    _mark_surface(solid, mesh.vertices[mesh.faces], lo, h)

    tick(0.3, "Repair: finding the outside")
    seed = np.zeros_like(solid)
    seed[[0, -1], :, :] = seed[:, [0, -1], :] = seed[:, :, [0, -1]] = True
    seed &= ~solid
    outside = binary_propagation(seed, mask=~solid)  # 6-connected: won't leak diagonally
    del seed
    solid = ~outside
    del outside

    tick(0.5, "Repair: rebuilding the surface")
    verts_all, faces_all, offset = [], [], 0
    nz = dims[2]
    for z0 in range(0, nz - 1, SLAB):
        z1 = min(z0 + SLAB, nz - 1)  # this slab owns cubes between layers z0..z1
        a, b = max(0, z0 - HALO), min(nz, z1 + 1 + HALO)
        field = gaussian_filter(solid[:, :, a:b].astype(np.float32), SIGMA, mode="constant")
        field = field[:, :, z0 - a : z1 - a + 1]
        if field.max() < LEVEL or field.min() > LEVEL:
            continue
        # Nudge off LEVEL: blurred binary data hits round values exactly, which
        # makes zero-area triangles that break watertightness.
        v, f, _, _ = marching_cubes(field, level=LEVEL + 1e-4, spacing=(h, h, h))
        v[:, 2] += z0 * h
        verts_all.append(v + lo)
        faces_all.append(f + offset)
        offset += len(v)
        tick(0.5 + 0.4 * z1 / nz, "Repair: rebuilding the surface")
    del solid
    if not verts_all:
        raise MeshError("Repair produced an empty model")

    tick(0.92, "Repair: checking the result")
    tm = trimesh.Trimesh(np.vstack(verts_all), np.vstack(faces_all), process=True)
    tm.merge_vertices(digits_vertex=6)
    tm.update_faces(tm.nondegenerate_faces())
    tm.remove_unreferenced_vertices()
    if not tm.is_watertight:
        raise MeshError("Automatic repair could not produce a closed surface")
    if tm.volume < 0:
        tm.invert()
    m = trimesh_to_manifold(tm)
    if m.is_empty():
        raise MeshError("Automatic repair produced invalid geometry")
    # Marching cubes over-tessellates flat areas; trim triangles well below voxel size.
    return m.simplify(h * 0.15)


def repaired_pitch_note(final_pitch: float) -> str:
    note = f"repair resolution {final_pitch:.1f} mm at final size"
    if final_pitch > 2.5:
        note += (
            " (coarser than ideal for a model this large; details under "
            f"~{math.ceil(final_pitch)} mm will soften. Repairing in a slicer first keeps them sharper)"
        )
    return note

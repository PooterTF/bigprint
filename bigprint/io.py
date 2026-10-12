# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Michael Christopher Stone
"""Mesh loading, repair, conversion between trimesh and manifold3d, and STL export."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import trimesh
from manifold3d import Manifold, Mesh, OpType


class MeshError(RuntimeError):
    pass


class NeedsRepair(MeshError):
    """The model has damage the shell-by-shell loader can't fix.

    `mesh` is the cleaned-up input, ready for repair.voxel_repair; `problem`
    describes the damage in plain words.
    """

    def __init__(self, name: str, problem: str, mesh: trimesh.Trimesh):
        self.name, self.problem, self.mesh = name, problem, mesh
        super().__init__(
            f"{name} has damage that needs repairing: {problem}. Turn on automatic repair, or repair "
            "the model first: in Bambu Studio or OrcaSlicer right-click it and choose Fix Model "
            "(Windows), or use Blender's 3D-Print Toolbox or Meshmixer, and export an STL."
        )


def trimesh_to_manifold(tm: trimesh.Trimesh) -> Manifold:
    mesh = Mesh(
        vert_properties=np.ascontiguousarray(tm.vertices, dtype=np.float32),
        tri_verts=np.ascontiguousarray(tm.faces, dtype=np.uint32),
    )
    return Manifold(mesh)


def manifold_to_trimesh(m: Manifold) -> trimesh.Trimesh:
    mesh = m.to_mesh()
    verts = np.asarray(mesh.vert_properties)[:, :3]
    faces = np.asarray(mesh.tri_verts)
    return trimesh.Trimesh(vertices=verts, faces=faces, process=False)


# Fraction of a model's triangles that may be dropped as unrepairable debris
# before loading fails instead of silently losing visible parts.
MAX_DROPPED_FRACTION = 0.02


def _defects(part: trimesh.Trimesh) -> tuple[int, int]:
    """(open edges, non-manifold edges) of a shell."""
    _, counts = np.unique(part.edges_sorted, axis=0, return_counts=True)
    return int((counts == 1).sum()), int((counts > 2).sum())


def load_model(path: str | Path, log=print) -> Manifold:
    """Load a mesh file and return a single watertight Manifold.

    Each connected shell is repaired independently and the shells are unioned,
    which resolves the overlapping-parts layout common in downloaded STLs.

    Only shells that are closed and consistently oriented are handed to
    manifold3d: given a broken shell it can crash the whole process (access
    violation) instead of reporting an error. Small unrepairable fragments are
    dropped with a note; if a meaningful part of the model is unrepairable,
    loading stops with an explanation instead.
    """
    loaded = trimesh.load(str(path), force="mesh")
    if not isinstance(loaded, trimesh.Trimesh) or len(loaded.faces) == 0:
        raise MeshError(f"No triangles found in {Path(path).name}")

    loaded.merge_vertices()
    loaded.update_faces(loaded.nondegenerate_faces())
    loaded.update_faces(loaded.unique_faces())
    loaded.remove_unreferenced_vertices()
    total = len(loaded.faces)

    shells: list[Manifold] = []
    broken: list[trimesh.Trimesh] = []
    for part in loaded.split(only_watertight=False):
        if len(part.faces) < 4:
            broken.append(part)
            continue
        if not part.is_watertight:
            trimesh.repair.fill_holes(part)
        if not part.is_watertight:
            broken.append(part)
            continue
        trimesh.repair.fix_normals(part)
        if not part.is_winding_consistent:
            broken.append(part)
            continue
        m = trimesh_to_manifold(part)
        if m.is_empty() or m.volume() <= 0:
            broken.append(part)
            continue
        shells.append(m)

    dropped = sum(len(b.faces) for b in broken)
    if dropped > MAX_DROPPED_FRACTION * total or not shells:
        worst = max(broken, key=lambda b: len(b.faces))
        holes, nonmanifold = _defects(worst)
        size = " x ".join(f"{v:.0f}" for v in worst.extents)
        problems = []
        if holes:
            problems.append(f"{holes:,} open edges (holes)")
        if nonmanifold:
            problems.append(f"{nonmanifold:,} edges shared by 3+ triangles (overlapping or stitched surfaces)")
        raise NeedsRepair(
            Path(path).name,
            f"{dropped / total:.0%} of its triangles are in broken parts; the largest "
            f"({len(worst.faces):,} triangles, {size} mm) has {' and '.join(problems) or 'invalid geometry'}",
            loaded,
        )
    if broken:
        log(f"Note: dropped {len(broken):,} tiny broken fragment(s) ({dropped:,} triangles, "
            f"{dropped / total:.1%} of the model) that could not be repaired")

    return Manifold.batch_boolean(shells, OpType.Add) if len(shells) > 1 else shells[0]


def save_stl(m: Manifold, path: str | Path) -> None:
    manifold_to_trimesh(m).export(str(path))


def clean(m: Manifold, tolerance: float = 0.005) -> Manifold:
    """Collapse sliver triangles and near-duplicate vertices left by booleans.

    Manifold keeps distinct vertices even when they are microns apart, but STL
    stores bare positions, so slicers re-merge them and can find bad edges.
    """
    return m.simplify(tolerance)


def survives_stl(m: Manifold) -> bool:
    """True if the mesh is still watertight once written as STL and read back,
    i.e. as a slicer will see it."""
    data = trimesh.exchange.stl.export_stl(manifold_to_trimesh(m))
    back = trimesh.load(trimesh.util.wrap_as_stream(data), file_type="stl")
    return bool(back.is_watertight)

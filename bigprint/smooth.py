# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Michael Christopher Stone
"""Smoothing of low-poly models after scaling.

Scaling a low-resolution STL up to life size makes its flat triangles
visible. Smoothing fits curved surfaces through the existing triangles,
using the angles between neighbouring faces, then subdivides those curves
into small triangles. Edges where neighbouring faces meet more sharply than
`sharp_angle` stay crisp (brims, soles, cuffs). It cannot restore detail the
model never had.
"""

from __future__ import annotations

import math

import numpy as np
import trimesh
from manifold3d import Manifold

from .io import manifold_to_trimesh, trimesh_to_manifold

COPLANAR_RAD = 1e-3


def _keep_flat_quads_flat(tm: trimesh.Trimesh, sharp_angle: float) -> trimesh.Trimesh:
    """Split flat two-triangle faces that have a sharp edge, so they stay flat.

    manifold3d treats two coplanar triangles as a quad patch and bends it during
    smoothing; faces of three or more coplanar triangles stay flat. A rectangle
    bordered by a sharp edge (a cube side, a statue's base) is meant to be flat,
    so each of its triangles is split into three around its centroid. Quads on a
    smoothly curving surface (common in Blender exports) have no sharp edge and
    are left to be smoothed.
    """
    adj, ang = tm.face_adjacency, tm.face_adjacency_angles
    groups = trimesh.graph.connected_components(adj[ang < COPLANAR_RAD], nodes=np.arange(len(tm.faces)), min_len=1)
    pairs = [g for g in groups if len(g) == 2]
    if not pairs:
        return tm
    sharp = math.radians(sharp_angle)
    group_of = np.full(len(tm.faces), -1)
    for i, g in enumerate(pairs):
        group_of[g] = i
    has_sharp_edge = np.zeros(len(pairs), dtype=bool)
    for (f1, f2), a in zip(adj, ang):
        if a > sharp:
            for f, other in ((f1, f2), (f2, f1)):
                g = group_of[f]
                if g >= 0 and group_of[other] != g:
                    has_sharp_edge[g] = True
    pick = np.concatenate([g for g, s in zip(pairs, has_sharp_edge) if s] or [np.array([], dtype=int)])
    if len(pick) == 0:
        return tm
    faces, verts = tm.faces, tm.vertices
    centres = verts[faces[pick]].mean(axis=1)
    ci = np.arange(len(verts), len(verts) + len(pick))
    a, b, c = faces[pick].T
    split = np.vstack([np.column_stack([a, b, ci]), np.column_stack([b, c, ci]), np.column_stack([c, a, ci])])
    keep = np.setdiff1d(np.arange(len(faces)), pick)
    return trimesh.Trimesh(np.vstack([verts, centres]), np.vstack([faces[keep], split]), process=False)


def smooth_model(m: Manifold, sharp_angle: float = 60.0, edge_length: float = 5.0) -> Manifold:
    """Return a smoothed copy of `m` whose triangles are at most ~`edge_length` mm.

    Uses refine_to_length rather than refine_to_tolerance, which crashes in
    manifold3d 3.5.x.
    """
    if edge_length <= 0:
        raise ValueError("--smooth-edge must be greater than 0")
    if not 0 < sharp_angle <= 180:
        raise ValueError("--smooth-angle must be between 0 and 180 degrees")
    prepared = trimesh_to_manifold(_keep_flat_quads_flat(manifold_to_trimesh(m), sharp_angle))
    if prepared.is_empty():
        prepared = m
    smoothed = prepared.smooth_out(sharp_angle, 0.0).refine_to_length(edge_length)
    if smoothed.is_empty() or smoothed.volume() <= 0:
        raise ValueError("Smoothing failed on this model; try again without --smooth")
    return smoothed

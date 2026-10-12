# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Michael Christopher Stone
"""Orientation and scaling of the input model. All units are millimetres."""

from __future__ import annotations

import re

from manifold3d import Manifold

_UNITS = {"mm": 1.0, "cm": 10.0, "m": 1000.0, "in": 25.4, '"': 25.4, "ft": 304.8, "'": 304.8}


def parse_length(text: str | float) -> float:
    """Parse '1.8m', '180cm', '72in', '6ft', or a bare number (mm)."""
    if isinstance(text, (int, float)):
        return float(text)
    match = re.fullmatch(r"\s*([0-9]*\.?[0-9]+)\s*([a-zA-Z\"']*)\s*", text)
    if not match:
        raise ValueError(f"Cannot parse length: {text!r}")
    value, unit = match.groups()
    unit = unit.lower() or "mm"
    if unit not in _UNITS:
        raise ValueError(f"Unknown unit {unit!r} in {text!r} (use mm, cm, m, in, ft)")
    return float(value) * _UNITS[unit]


def orient_up(m: Manifold, up: str) -> Manifold:
    """Rotate so the given model axis points along +Z."""
    up = up.lower()
    if up == "z":
        return m
    if up == "y":
        return m.rotate([90, 0, 0])
    if up == "-y":
        return m.rotate([-90, 0, 0])
    if up == "x":
        return m.rotate([0, -90, 0])
    if up == "-z":
        return m.rotate([180, 0, 0])
    raise ValueError(f"Unsupported up axis {up!r} (use z, y, -y, x, -z)")


def extents(m: Manifold) -> tuple[float, float, float]:
    x0, y0, z0, x1, y1, z1 = m.bounding_box()
    return x1 - x0, y1 - y0, z1 - z0


def oriented_extents(ext, up: str) -> tuple[float, float, float]:
    """Model extents after orient_up(…, up), without rotating anything."""
    x, y, z = (float(v) for v in ext)
    up = up.lower()
    if up in ("y", "-y"):
        return x, z, y
    if up == "x":
        return z, y, x
    return x, y, z


def target_factor(ext, *, factor=None, height=None, width=None, depth=None) -> float:
    """Uniform scale factor for extents (x, y, z) to hit the requested size."""
    if sum(v is not None for v in (factor, height, width, depth)) > 1:
        raise ValueError("Specify only one of scale/height/width/depth")
    ex, ey, ez = ext
    if height is not None:
        return height / ez
    if width is not None:
        return width / ex
    if depth is not None:
        return depth / ey
    return 1.0 if factor is None else factor


def scale_model(
    m: Manifold,
    *,
    factor: float | None = None,
    height: float | None = None,
    width: float | None = None,
    depth: float | None = None,
) -> tuple[Manifold, float]:
    """Uniformly scale by an explicit factor or so one dimension hits a target size.

    Returns the scaled model (re-seated on z=0, centred in XY) and the factor used.
    """
    factor = target_factor(extents(m), factor=factor, height=height, width=width, depth=depth)
    m = m.scale([factor] * 3)
    x0, y0, z0, x1, y1, _ = m.bounding_box()
    m = m.translate([-(x0 + x1) / 2, -(y0 + y1) / 2, -z0])
    return m, factor

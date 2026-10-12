# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Michael Christopher Stone
"""Minimal 3MF writer with PrusaSlicer per-object settings.

PrusaSlicer reads `Metadata/Slic3r_PE_model.config`, so each piece opens with
its recommended perimeters/infill already applied. Other slicers load the
geometry and ignore that file.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from xml.sax.saxutils import quoteattr

import numpy as np

from .io import manifold_to_trimesh

CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
 <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
 <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>
 <Default Extension="config" ContentType="text/xml"/>
</Types>
"""

RELS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>
</Relationships>
"""


def write_3mf(path: str | Path, objects: list[dict]) -> None:
    """objects: dicts with keys name, manifold (print-oriented), settings (dict str->str)."""
    model_parts = [
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<model unit="millimeter" xml:lang="en-US" '
        'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">\n'
        ' <metadata name="Application">bigprint</metadata>\n <resources>\n'
    ]
    config = ['<?xml version="1.0" encoding="UTF-8"?>\n<config>\n']
    build = [" <build>\n"]

    x_cursor = 0.0
    for i, obj in enumerate(objects, start=1):
        tm = manifold_to_trimesh(obj["manifold"])
        verts, faces = np.asarray(tm.vertices), np.asarray(tm.faces)
        vtxt = "\n".join(f'     <vertex x="{x:.4f}" y="{y:.4f}" z="{z:.4f}"/>' for x, y, z in verts)
        ttxt = "\n".join(f'     <triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in faces)
        name = quoteattr(obj["name"])
        model_parts.append(
            f'  <object id="{i}" name={name} type="model">\n   <mesh>\n    <vertices>\n{vtxt}\n    </vertices>\n'
            f"    <triangles>\n{ttxt}\n    </triangles>\n   </mesh>\n  </object>\n"
        )
        # Lay objects out in a row so they don't overlap; use the slicer's Arrange.
        x0, y0, z0 = verts.min(0)
        x1 = verts.max(0)[0]
        tx = x_cursor - x0
        x_cursor += (x1 - x0) + 10
        build.append(f'  <item objectid="{i}" transform="1 0 0 0 1 0 0 0 1 {tx:.3f} {-y0:.3f} {-z0:.3f}"/>\n')

        config.append(f' <object id="{i}" instances_count="1">\n')
        config.append(f'  <metadata type="object" key="name" value={name}/>\n')
        for key, value in obj.get("settings", {}).items():
            config.append(f'  <metadata type="object" key="{key}" value={quoteattr(str(value))}/>\n')
        config.append(f'  <volume firstid="0" lastid="{len(faces) - 1}">\n')
        config.append(f'   <metadata type="volume" key="name" value={name}/>\n  </volume>\n </object>\n')

    model_parts.append(" </resources>\n")
    model_parts.extend(build)
    model_parts.append(" </build>\n</model>\n")
    config.append("</config>\n")

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("_rels/.rels", RELS)
        z.writestr("3D/3dmodel.model", "".join(model_parts))
        z.writestr("Metadata/Slic3r_PE_model.config", "".join(config))

# SPDX-License-Identifier: AGPL-3.0-or-later
import subprocess
import sys
import zipfile

import numpy as np
import pytest
import trimesh
from manifold3d import Manifold

from bigprint.io import NeedsRepair, load_model, manifold_to_trimesh, trimesh_to_manifold
from bigprint.labels import GLYPHS, text_outline
from bigprint.pipeline import Options, run
from bigprint.scale import parse_length
from bigprint.smooth import smooth_model
from bigprint.split import plan_cuts, split_model
from bigprint.threemf import write_3mf

BED = (256.0, 256.0, 256.0)


def _save(m: Manifold, path) -> str:
    manifold_to_trimesh(m).export(str(path))
    return str(path)


# ---------------------------------------------------------------- basics


@pytest.mark.parametrize(
    "text,mm", [("1.8m", 1800), ("180cm", 1800), ("6ft", 1828.8), ("72in", 1828.8), ("250", 250), (12.5, 12.5)]
)
def test_parse_length(text, mm):
    assert parse_length(text) == pytest.approx(mm)


def test_glyphs_render():
    for ch in GLYPHS:
        assert text_outline(ch, 10).area > 0, ch


@pytest.mark.parametrize("ext", ["stl", "obj", "ply", "3mf"])
def test_every_input_format_loads(tmp_path, ext):
    # Regression: 3MF needs lxml at runtime (trimesh imports it lazily).
    path = tmp_path / f"ball.{ext}"
    trimesh.creation.icosphere(subdivisions=3, radius=50).export(str(path))
    m = load_model(path, log=lambda *_: None)
    assert m.volume() == pytest.approx(4 / 3 * np.pi * 50**3, rel=0.02)


# ---------------------------------------------------------------- cutting


def test_solid_split_conserves_volume_and_fits():
    box = Manifold.cube([600, 300, 300])
    plan = plan_cuts(box, BED)
    pieces, joints = split_model(box, plan)
    assert sum(p.solid.volume() for p in pieces) == pytest.approx(box.volume(), rel=1e-6)
    for p in pieces:
        b = p.solid.bounding_box()
        assert all(b[k + 3] - b[k] <= BED[k] for k in range(3))
    # 3 x 2 x 2 grid -> 12 pieces; interior contacts: 2*2*2 + 3*1*2 + 3*2*1 = 20
    assert len(pieces) == 12
    assert len(joints) == 20


def test_cut_optimiser_prefers_narrow_waist():
    # Two blocks joined by a thin neck: the cut should land in the neck.
    body = Manifold.cube([100, 100, 200]) + Manifold.cube([100, 100, 200]).translate([0, 0, 220])
    neck = Manifold.cube([20, 20, 30]).translate([40, 40, 195])
    plan = plan_cuts(body + neck, BED)
    (z,) = plan.cuts[2]
    assert 200 <= z <= 220


# ---------------------------------------------------------------- full pipeline (solid)


@pytest.fixture(scope="module")
def solid_result(tmp_path_factory):
    path = _save(Manifold.cube([500, 200, 200]), tmp_path_factory.mktemp("m") / "box.stl")
    return run(path, Options(bed=BED, hollow="off"), log=lambda *_: None)


def test_dowels_pair_up(solid_result):
    r = solid_result
    assert r.joints and all(j.dowels for j in r.joints)
    # Every dowel removes material from both mating pieces.
    by_id = {fp.id: fp for fp in r.pieces}
    for j in r.joints:
        for fp_id in (j.neg, j.pos):
            fp = by_id[fp_id]
            assert fp.final.volume() < fp.piece.solid.volume()
    assert sum(r.pin_counts.values()) == sum(len(j.dowels) for j in r.joints)


def test_pieces_watertight_and_fit_bed(solid_result):
    for fp in solid_result.pieces:
        tm = manifold_to_trimesh(fp.printable())
        assert tm.is_watertight
        assert all(e <= b + 1e-6 for e, b in zip(tm.extents, BED))
        assert tm.bounds[0][2] == pytest.approx(0, abs=1e-6)  # sits on the bed


def test_every_piece_labelled(solid_result):
    assert not [w for w in solid_result.warnings if "label" in w]


def test_fill_basis_names_the_preset(solid_result):
    basis = solid_result.fill_basis
    assert "Display piece preset" in basis and "gyroid" in basis


# ---------------------------------------------------------------- hollowing


def test_hollow_sphere_drops_interior_and_saves_filament(tmp_path):
    sphere = Manifold.sphere(300, 96)
    path = _save(sphere, tmp_path / "sphere.stl")
    r = run(path, Options(bed=BED, hollow="on", optimise_cuts=False), log=lambda *_: None)
    assert r.dropped_cells >= 1  # the centre cell is entirely inside the hollow
    total = sum(fp.final.volume() for fp in r.pieces)
    assert total < 0.25 * sphere.volume()
    for fp in r.pieces:
        assert manifold_to_trimesh(fp.final).is_watertight
    dowelled = [j for j in r.joints if j.dowels]
    assert len(dowelled) >= 0.7 * len(r.joints)


# ---------------------------------------------------------------- 3MF output


def test_3mf_roundtrip(tmp_path):
    cube = Manifold.cube([10, 20, 30])
    out = tmp_path / "p.3mf"
    write_3mf(out, [{"name": "A1", "manifold": cube, "settings": {"perimeters": 3, "fill_density": "5%"}}])
    loaded = trimesh.load(str(out), force="mesh")
    assert np.allclose(sorted(loaded.extents), [10, 20, 30])
    with zipfile.ZipFile(out) as z:
        cfg = z.read("Metadata/Slic3r_PE_model.config").decode()
    assert 'key="fill_density" value="5%"' in cfg


# ---------------------------------------------------------------- smoothing


def test_smoothing_rounds_low_poly_and_keeps_sharp_edges():
    # A 20-face icosahedron "sphere" gets much closer to the true sphere volume.
    ico = trimesh.creation.icosphere(subdivisions=0, radius=100)
    true_vol = 4 / 3 * np.pi * 100**3
    m = trimesh_to_manifold(ico)
    s = smooth_model(m, sharp_angle=60, edge_length=5)
    assert s.num_tri() > 50 * m.num_tri()
    assert abs(s.volume() - true_vol) < abs(m.volume() - true_vol) / 2
    assert manifold_to_trimesh(s).is_watertight

    # 90 degree cube edges are sharper than 60 degrees, so the cube keeps its shape.
    cube = Manifold.cube([100, 100, 100])
    assert smooth_model(cube, 60, 5).volume() == pytest.approx(cube.volume(), rel=1e-3)


def test_smoothing_still_rounds_quad_meshes():
    # A low-poly UV sphere is made of flat quads; they must still be smoothed.
    uv = trimesh.creation.uv_sphere(radius=100, count=[8, 8])
    m = trimesh_to_manifold(uv)
    true_vol = 4 / 3 * np.pi * 100**3
    s = smooth_model(m, 60, 5)
    assert abs(s.volume() - true_vol) < abs(m.volume() - true_vol) / 2


def test_smooth_option_runs_end_to_end(tmp_path):
    ico = trimesh.creation.icosphere(subdivisions=1, radius=10)
    path = tmp_path / "ball.stl"
    ico.export(str(path))
    r = run(str(path), Options(bed=BED, height=400, smooth=True), log=lambda *_: None)
    assert r.pieces
    assert not [w for w in r.warnings if "surfaces touch" in w]


# ---------------------------------------------------------------- damaged models


def _broken_box() -> trimesh.Trimesh:
    """A box with a hole and a fin sharing an edge with 3 triangles: unrepairable shell by shell."""
    box = trimesh.creation.box((50, 50, 50))
    faces = box.faces[1:]  # open a hole
    a, b = box.faces[2][:2]  # an edge already shared by two triangles
    fin = len(box.vertices)
    verts = np.vstack([box.vertices, [[0, 0, 80]]])
    return trimesh.Trimesh(verts, np.vstack([faces, [[a, b, fin]]]), process=False)


def _load_in_subprocess(path) -> tuple[int, str]:
    """Load in a child process so a native crash fails the test instead of killing pytest."""
    code = (
        "import sys; from bigprint.io import load_model, MeshError\n"
        "try:\n    m = load_model(sys.argv[1], log=print); print('LOADED', round(m.volume()))\n"
        "except MeshError as e:\n    print('MESHERROR', e)\n"
    )
    r = subprocess.run([sys.executable, "-c", code, str(path)], capture_output=True, text=True, timeout=120)
    return r.returncode, r.stdout + r.stderr


def test_unrepairable_model_gives_clear_error_not_crash(tmp_path):
    path = tmp_path / "broken.stl"
    _broken_box().export(str(path))
    code, out = _load_in_subprocess(path)
    assert code == 0, f"loader crashed (exit {code}): {out}"
    assert "MESHERROR" in out and "Fix Model" in out


def test_tiny_broken_fragments_are_dropped(tmp_path):
    body = trimesh.creation.icosphere(subdivisions=4, radius=50)  # watertight
    debris = _broken_box()
    debris.apply_scale(0.01)
    debris.apply_translation([200, 0, 0])
    path = tmp_path / "debris.stl"
    trimesh.util.concatenate([body, debris]).export(str(path))
    code, out = _load_in_subprocess(path)
    assert code == 0, f"loader crashed (exit {code}): {out}"
    assert "LOADED" in out and "dropped 1" in out


def _damaged_sphere(r=50.0) -> trimesh.Trimesh:
    """Sphere with 40 holes and a duplicated overlapping patch (non-manifold edges)."""
    s = trimesh.creation.icosphere(subdivisions=5, radius=r)
    keep = np.ones(len(s.faces), bool)
    keep[np.random.default_rng(0).choice(len(s.faces), 40, replace=False)] = False
    return trimesh.Trimesh(s.vertices, np.vstack([s.faces[keep], s.faces[:300]]), process=False)


def test_voxel_repair_keeps_shape():
    from bigprint.repair import voxel_repair

    out = voxel_repair(_damaged_sphere(), pitch=1.0)
    tm = manifold_to_trimesh(out)
    assert tm.is_watertight
    radius = np.linalg.norm(tm.vertices, axis=1)
    assert abs(radius.mean() - 50) < 0.3  # within a third of a voxel on average
    assert out.volume() == pytest.approx(4 / 3 * np.pi * 50**3, rel=0.03)


def test_damaged_model_is_repaired_in_pipeline(tmp_path):
    # Loose fins and duplicate faces are cleaned up by the loader; a main body
    # with a hole it can't fill (next to a 3-triangle edge) needs the rebuild.
    path = tmp_path / "damaged.stl"
    _broken_box().export(str(path))
    logs = []
    r = run(str(path), Options(bed=BED, height=400), log=logs.append)
    assert any("repairing automatically" in line for line in logs)
    assert r.pieces and all(manifold_to_trimesh(fp.final).is_watertight for fp in r.pieces)

    with pytest.raises(NeedsRepair):
        run(str(path), Options(bed=BED, height=400, repair=False), log=lambda *_: None)

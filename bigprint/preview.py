# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Michael Christopher Stone
"""Self-contained HTML preview: assembled / exploded view of the pieces (three.js)."""

from __future__ import annotations

import base64
import colorsys
import json
from functools import lru_cache
from importlib import resources
from pathlib import Path

import numpy as np

from .pipeline import Result

MAX_PREVIEW_TRIS = 1_500_000


def _b64(arr: np.ndarray) -> str:
    return base64.b64encode(np.ascontiguousarray(arr).tobytes()).decode("ascii")


def _color(i: int, n: int) -> str:
    h = (i * 0.618033988749895) % 1.0
    r, g, b = colorsys.hls_to_rgb(h, 0.6, 0.55)
    return f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"


@lru_cache(maxsize=1)
def _import_map() -> str:
    """Import map pointing at the bundled three.js as data: URLs.

    Embedding the library makes every preview work offline, both inside the GUI
    and when a saved preview.html is opened on its own.
    """
    vendor = resources.files("bigprint").joinpath("static/vendor/three")

    def data_url(name: str) -> str:
        return "data:text/javascript;base64," + base64.b64encode(vendor.joinpath(name).read_bytes()).decode("ascii")

    return json.dumps({"imports": {
        "three": data_url("three.module.js"),
        "three/addons/OrbitControls.js": data_url("OrbitControls.js"),
    }})


def write_preview(result: Result, path: str | Path) -> None:
    total = sum(fp.final.num_tri() for fp in result.pieces)
    tol = 0.0
    if total > MAX_PREVIEW_TRIS:
        tol = max(result.size) / 2000  # coarse enough to keep the page light
    items = []
    for i, fp in enumerate(result.pieces):
        m = fp.final.simplify(tol) if tol else fp.final
        mesh = m.to_mesh()
        verts = np.asarray(mesh.vert_properties, dtype=np.float32)[:, :3]
        tris = np.asarray(mesh.tri_verts, dtype=np.uint32)
        b = fp.final.bounding_box()
        items.append(
            {
                "id": fp.id,
                "color": _color(i, len(result.pieces)),
                "pos": _b64(verts),
                "idx": _b64(tris),
                "center": [(b[0] + b[3]) / 2, (b[1] + b[4]) / 2, (b[2] + b[5]) / 2],
                "size": [round(s) for s in fp.placement.size],
                "grams": round(fp.fill.grams),
                "fill": f"{fp.fill.walls} walls, {fp.fill.infill_pct} {fp.fill.pattern}",
                "neighbors": sorted(set(fp.piece.neighbors)),
            }
        )
    data = {
        "pieces": items,
        "size": [round(s) for s in result.size],
        "grams": round(sum(fp.fill.grams for fp in result.pieces)),
        "basis": result.fill_basis,
        "source": Path(result.source).name,
    }
    html = TEMPLATE.replace("__IMPORTMAP__", _import_map()).replace("__DATA__", json.dumps(data))
    Path(path).write_text(html, encoding="utf-8")


TEMPLATE = r"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>bigprint preview</title>
<style>
  :root { --bg:#f4f4f2; --panel:#ffffff; --ink:#1d1d1f; --muted:#6b6b70; --line:#dedede; --accent:#2f6fde; }
  @media (prefers-color-scheme: dark) { :root { --bg:#17181a; --panel:#202124; --ink:#ececec; --muted:#9a9aa0; --line:#34353a; --accent:#6c9cff; } }
  html,body { margin:0; height:100%; background:var(--bg); color:var(--ink); font:14px system-ui,sans-serif; }
  #app { display:flex; height:100%; }
  #view { flex:1; position:relative; min-width:0; }
  #side { width:300px; overflow:auto; background:var(--panel); border-left:1px solid var(--line); padding:16px; box-sizing:border-box; }
  h1 { font-size:16px; margin:0 0 4px; } .muted { color:var(--muted); font-size:12px; }
  .row { display:flex; align-items:center; gap:8px; margin:14px 0; } input[type=range]{ flex:1; }
  table { width:100%; border-collapse:collapse; font-size:12px; margin-top:8px; }
  td,th { text-align:left; padding:4px 3px; border-bottom:1px solid var(--line); font-variant-numeric:tabular-nums; }
  tr.p { cursor:pointer; } tr.p:hover, tr.sel { background:color-mix(in srgb, var(--accent) 15%, transparent); }
  .sw { display:inline-block; width:10px; height:10px; border-radius:2px; margin-right:6px; vertical-align:-1px; }
  .tag { position:absolute; transform:translate(-50%,-50%); font:600 11px system-ui; background:rgba(0,0,0,.65); color:#fff; padding:1px 5px; border-radius:3px; pointer-events:none; white-space:nowrap; }
  #tip { position:absolute; left:12px; bottom:12px; background:var(--panel); border:1px solid var(--line); border-radius:6px; padding:8px 10px; font-size:12px; display:none; }
  button { font:inherit; padding:4px 10px; border:1px solid var(--line); border-radius:5px; background:var(--panel); color:var(--ink); cursor:pointer; }
  @media (max-width:700px){ #app{flex-direction:column;} #side{width:auto; height:45%; border-left:0; border-top:1px solid var(--line);} }
</style>
<script type="importmap">__IMPORTMAP__</script>
</head><body>
<div id="app"><div id="view"><div id="tip"></div></div>
<div id="side">
  <h1 id="title"></h1><div class="muted" id="summary"></div>
  <div class="muted" id="basis" style="margin-top:6px"></div>
  <div class="row"><span class="muted">Explode</span><input id="explode" type="range" min="0" max="100" value="25"></div>
  <div class="row"><label><input type="checkbox" id="tags" checked> Show IDs</label><button id="all">Show all</button></div>
  <table><thead><tr><th>ID</th><th>Print size (mm)</th><th>g</th></tr></thead><tbody id="list"></tbody></table>
</div></div>
<script type="module">
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/OrbitControls.js';
const DATA = __DATA__;
const dec = (s, T) => { const b = atob(s), u = new Uint8Array(b.length); for (let i=0;i<b.length;i++) u[i]=b.charCodeAt(i); return new T(u.buffer); };

const view = document.getElementById('view');
const renderer = new THREE.WebGLRenderer({ antialias:true, alpha:true });
renderer.setPixelRatio(devicePixelRatio); view.appendChild(renderer.domElement);
const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(35, 1, 1, 1e6); camera.up.set(0,0,1);
scene.add(new THREE.HemisphereLight(0xffffff, 0x666677, 1.6));
const sun = new THREE.DirectionalLight(0xffffff, 1.8); sun.position.set(1,-1.5,2); scene.add(sun);
const controls = new OrbitControls(camera, renderer.domElement);

const S = DATA.size, mid = new THREE.Vector3(0, 0, S[2]/2), R = Math.max(...S);
camera.position.set(R*1.2, -R*1.8, R*1.1); controls.target.copy(mid); controls.update();
const grid = new THREE.GridHelper(R*2, 20, 0x888888, 0x888888); grid.rotation.x = Math.PI/2; grid.material.opacity=.25; grid.material.transparent=true; scene.add(grid);

const meshes = [], tags = [];
for (const p of DATA.pieces) {
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.BufferAttribute(dec(p.pos, Float32Array), 3));
  g.setIndex(new THREE.BufferAttribute(dec(p.idx, Uint32Array), 1));
  g.computeVertexNormals();
  const mat = new THREE.MeshStandardMaterial({ color:p.color, roughness:.65, metalness:0, flatShading:true });
  const m = new THREE.Mesh(g, mat); m.userData = p; scene.add(m); meshes.push(m);
  const t = document.createElement('div'); t.className='tag'; t.textContent=p.id; view.appendChild(t); tags.push(t);
}

document.getElementById('title').textContent = DATA.source;
document.getElementById('summary').textContent = `${S[0]} × ${S[1]} × ${S[2]} mm · ${DATA.pieces.length} pieces · ~${(DATA.grams/1000).toFixed(2)} kg`;
document.getElementById('basis').textContent = `Estimate based on: ${DATA.basis}`;
const list = document.getElementById('list');
DATA.pieces.forEach((p,i) => {
  const tr = document.createElement('tr'); tr.className='p';
  tr.innerHTML = `<td><span class="sw" style="background:${p.color}"></span>${p.id}</td><td>${p.size.join('×')}</td><td>${p.grams}</td>`;
  tr.title = `${p.fill}\nTouches: ${p.neighbors.join(', ') || '—'}`;
  tr.onclick = () => isolate(i, tr); list.appendChild(tr);
});
function isolate(i, tr) {
  meshes.forEach((m,k) => m.visible = (k===i || DATA.pieces[i].neighbors.includes(DATA.pieces[k].id)));
  meshes.forEach((m,k) => m.material.opacity = k===i ? 1 : .35);
  meshes.forEach((m,k) => m.material.transparent = k!==i);
  document.querySelectorAll('tr.sel').forEach(e=>e.classList.remove('sel')); tr.classList.add('sel');
}
document.getElementById('all').onclick = () => { meshes.forEach(m => { m.visible=true; m.material.opacity=1; m.material.transparent=false; }); document.querySelectorAll('tr.sel').forEach(e=>e.classList.remove('sel')); };

const explode = document.getElementById('explode');
function applyExplode() {
  const f = explode.value/100;
  meshes.forEach(m => { const c = m.userData.center; m.position.set((c[0]-mid.x)*f, (c[1]-mid.y)*f, (c[2]-mid.z)*f); });
}
explode.oninput = applyExplode; applyExplode();

const showTags = document.getElementById('tags');
const tip = document.getElementById('tip'), ray = new THREE.Raycaster(), ptr = new THREE.Vector2();
renderer.domElement.addEventListener('pointermove', e => {
  const r = renderer.domElement.getBoundingClientRect();
  ptr.set(((e.clientX-r.left)/r.width)*2-1, -((e.clientY-r.top)/r.height)*2+1);
  ray.setFromCamera(ptr, camera);
  const hit = ray.intersectObjects(meshes.filter(m=>m.visible))[0];
  if (hit) { const p = hit.object.userData; tip.style.display='block'; tip.innerHTML = `<b>${p.id}</b> · ${p.size.join('×')} mm · ~${p.grams} g<br>${p.fill}<br>Touches: ${p.neighbors.join(', ')||'—'}`; }
  else tip.style.display='none';
});

function resize(){ const w=view.clientWidth, h=view.clientHeight; renderer.setSize(w,h); camera.aspect=w/h; camera.updateProjectionMatrix(); }
addEventListener('resize', resize); resize();
const v = new THREE.Vector3();
renderer.setAnimationLoop(() => {
  controls.update(); renderer.render(scene, camera);
  const w = view.clientWidth, h = view.clientHeight;
  meshes.forEach((m,i) => {
    const c = m.userData.center; v.set(c[0],c[1],c[2]).add(m.position).project(camera);
    const t = tags[i]; t.style.display = (showTags.checked && m.visible && v.z<1) ? 'block' : 'none';
    t.style.left = ((v.x+1)/2*w)+'px'; t.style.top = ((1-v.y)/2*h)+'px';
  });
});
</script></body></html>
"""

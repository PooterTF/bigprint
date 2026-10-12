# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest
import trimesh

from bigprint.gui import Session, make_handler


@pytest.fixture()
def server():
    session = Session()
    port = [0]
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(session, port))
    port[0] = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port[0]}", session
    httpd.shutdown()
    session.close()


def call(base, path, body=None, headers=None, raw=None):
    h = {"X-Bigprint": "1", **(headers or {})}
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    if body is not None:
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, headers=h, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def wait(base, timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        _, body = call(base, "/api/job")
        data = json.loads(body)
        if data["job"]["state"] != "running":
            return data
        time.sleep(0.2)
    raise TimeoutError


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


SETTINGS = {
    "size_mode": "height", "size_value": "300", "size_unit": "mm", "printer": "bambu-a1-mini",
    "bed": ["250", "250", "250"], "purpose": "display", "hollow": "auto", "wall": "4", "material": "pla",
    "price": "20", "dowels": True, "dowel_d": "6", "dowel_depth": "15", "clearance": "0.4", "labels": True,
    "smooth": False, "smooth_angle": "60", "smooth_edge": "5", "up": "z", "margin": "5", "nozzle": "0.4",
    "merge_small": True, "even_cuts": False,
}


def _upload_ball(base):
    stl = trimesh.exchange.stl.export_stl(trimesh.creation.icosphere(subdivisions=3, radius=10))
    return json.loads(call(base, "/api/upload", raw=stl, headers={"X-Filename": "ball.stl"})[1])


def test_page_and_security(server):
    base, _ = server
    status, body = call(base, "/")
    assert status == 200 and b"<title>bigprint</title>" in body
    # POST without the custom header is refused (blocks cross-site requests).
    req = urllib.request.Request(base + "/api/cancel", data=b"{}", method="POST")
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 403
    # Foreign Host header is refused (blocks DNS rebinding).
    status, _ = call(base, "/api/config", headers={"Host": "evil.example"})
    assert status == 403


def test_config_includes_source_and_support_links(server):
    base, _ = server
    cfg = json.loads(call(base, "/api/config")[1])
    assert cfg["project"]["source"].startswith("https://")
    assert cfg["project"]["support"]


def test_preview_then_save(server, tmp_path):
    base, _ = server
    stl = trimesh.exchange.stl.export_stl(trimesh.creation.box((100, 60, 150)))
    status, body = call(base, "/api/upload", raw=stl, headers={"X-Filename": "box.stl"})
    assert status == 200, body
    model = json.loads(body)
    assert model["triangles"] == 12 and sorted(model["extents"]) == pytest.approx([60, 100, 150])

    settings = {**SETTINGS, "model_id": model["id"]}
    # Saving before any preview is refused.
    status, body = call(base, "/api/save", {"settings": settings, "out": str(tmp_path / "out")})
    assert status == 400

    status, _ = call(base, "/api/preview", {"settings": settings})
    assert status == 200
    data = wait(base)
    assert data["job"]["state"] == "done", data["job"]
    sm = data["summary"]
    assert sm["pieces"] >= 2 and sm["size"][2] == 300 and sm["cost"] > 0
    assert "gyroid" in sm["basis"]
    status, body = call(base, "/api/preview")
    assert status == 200 and b"three" in body

    # Changed settings must be previewed again before saving.
    status, body = call(base, "/api/save", {"settings": {**settings, "wall": "5"}, "out": str(tmp_path / "out")})
    assert status == 400 and b"changed" in body

    status, _ = call(base, "/api/save", {"settings": settings, "out": str(tmp_path / "out")})
    assert status == 200
    data = wait(base)
    assert data["job"]["state"] == "done", data["job"]
    out = tmp_path / "out"
    assert (out / "project.3mf").exists() and (out / "report.md").exists()
    assert len(list((out / "pieces").glob("*.stl"))) == sm["pieces"]


def test_bad_settings_are_reported(server):
    base, _ = server
    stl = trimesh.exchange.stl.export_stl(trimesh.creation.box((10, 10, 10)))
    model = json.loads(call(base, "/api/upload", raw=stl, headers={"X-Filename": "c.stl"})[1])
    status, body = call(base, "/api/preview", {"settings": {**SETTINGS, "model_id": model["id"], "size_value": "abc"}})
    assert status == 400 and b"size value must be a number" in body


def test_cancel_is_immediate_and_next_run_can_start(server):
    base, session = server
    model = _upload_ball(base)
    # Fine smoothing of a 1.5 m ball is one long native call: a cooperative
    # cancel could not interrupt it.
    slow = {**SETTINGS, "model_id": model["id"], "size_value": "1500", "hollow": "on",
            "smooth": True, "smooth_edge": "1.5"}
    assert call(base, "/api/preview", {"settings": slow})[0] == 200
    time.sleep(1.5)
    worker = session.worker
    t = time.time()
    assert call(base, "/api/cancel", {})[0] == 200
    data = json.loads(call(base, "/api/job")[1])
    assert time.time() - t < 3
    assert data["job"]["state"] == "cancelled"
    assert data["summary"] is None
    assert not worker.proc.is_alive()  # the process is really gone

    # A new preview is accepted immediately and completes.
    assert call(base, "/api/preview", {"settings": {**SETTINGS, "model_id": model["id"]}})[0] == 200
    data = wait(base)
    assert data["job"]["state"] == "done" and data["summary"]["pieces"] >= 1


def test_worker_crash_is_reported(server):
    base, session = server
    model = _upload_ball(base)
    settings = {**SETTINGS, "model_id": model["id"], "size_value": "1500", "hollow": "on"}
    assert call(base, "/api/preview", {"settings": settings})[0] == 200
    time.sleep(1.0)
    session.worker.proc.kill()  # simulate a crash inside a native library
    data = wait(base)
    assert data["job"]["state"] == "error"
    assert "stopped unexpectedly" in data["job"]["error"]
    # The server is still usable.
    assert call(base, "/api/preview", {"settings": {**settings, "size_value": "300"}})[0] == 200
    assert wait(base)["job"]["state"] == "done"


def test_quit_stops_server_workers_and_temp_files():
    session = Session()
    port = [0]
    stopped = threading.Event()
    httpd = None

    def on_quit():
        threading.Thread(target=lambda: (httpd.shutdown(), stopped.set()), daemon=True).start()

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(session, port, on_quit=on_quit))
    port[0] = httpd.server_address[1]
    served = threading.Thread(target=httpd.serve_forever, daemon=True)
    served.start()
    base = f"http://127.0.0.1:{port[0]}"
    spare = session.spare
    assert spare is not None and spare.alive()

    status, body = call(base, "/api/quit", {})
    assert status == 200 and json.loads(body)["ok"]
    assert stopped.wait(10)
    served.join(5)
    assert not served.is_alive()
    httpd.server_close()
    session.close()  # what serve() does once serve_forever returns
    assert not spare.proc.is_alive()
    assert not session.workdir.exists()


def test_quit_needs_the_custom_header(server):
    base, _ = server
    req = urllib.request.Request(base + "/api/quit", data=b"{}", method="POST")
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 403


def test_ping_and_single_instance_detection(server):
    from bigprint.gui import _running_instance

    base, _ = server
    status, body = call(base, "/api/ping")
    assert status == 200 and json.loads(body)["app"] == "bigprint"
    assert _running_instance(int(base.rsplit(":", 1)[1]))
    assert not _running_instance(_free_port())


def test_idle_shutdown_without_any_tab():
    import bigprint.gui as gui

    port = _free_port()
    t = time.time()
    done = threading.Event()
    threading.Thread(
        target=lambda: (gui.serve(port, open_browser=False, idle_minutes=0.05), done.set()), daemon=True
    ).start()
    assert done.wait(30), "server did not stop on its own"
    assert 2 < time.time() - t < 30


def test_preview_page_works_offline(server):
    base, _ = server
    stl = trimesh.exchange.stl.export_stl(trimesh.creation.box((100, 60, 150)))
    model = json.loads(call(base, "/api/upload", raw=stl, headers={"X-Filename": "box.stl"})[1])
    call(base, "/api/preview", {"settings": {**SETTINGS, "model_id": model["id"]}})
    assert wait(base)["job"]["state"] == "done"
    html = call(base, "/api/preview")[1].decode()
    assert "data:text/javascript;base64," in html  # three.js is embedded
    assert "cdn.jsdelivr" not in html and "unpkg.com" not in html

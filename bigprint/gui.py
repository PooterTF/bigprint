# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Michael Christopher Stone
"""Local web GUI: `bigprint-gui` starts a server on 127.0.0.1 and opens the browser.

The page (static/gui.html) uploads a model, sends settings as JSON, and polls a
background job. A preview run keeps its Result in memory, so "Save print files"
writes the outputs without recomputing.

Only this machine can connect (127.0.0.1). POST requests must carry an
X-Bigprint header, which other websites can't send cross-origin without a CORS
preflight this server never approves, and the Host header must be local,
which blocks DNS-rebinding.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
import webbrowser
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

import numpy as np
import trimesh

from .connectors import DowelSpec
from .pipeline import Options, Result, run
from .preview import write_preview
from .printers import DEFAULT_PRINTER, PRINTERS
from .project import LICENSE, SOURCE_URL, SUPPORT_LINKS
from .scale import parse_length
from .strategy import MATERIAL_DENSITY, PURPOSES

MAX_UPLOAD = 1 << 30  # 1 GiB
IDLE_MINUTES = 10  # quit when no page has been in touch this long (and nothing is running)
UNITS = ("mm", "cm", "m", "in", "ft")


class SettingsError(ValueError):
    pass


# ---------------------------------------------------------------- settings


def _num(d: dict, key: str, lo: float | None = None, hi: float | None = None) -> float:
    try:
        v = float(d[key])
    except (KeyError, TypeError, ValueError):
        raise SettingsError(f"{key.replace('_', ' ')} must be a number")
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        raise SettingsError(f"{key.replace('_', ' ')} must be between {lo:g} and {hi:g}")
    return v


def options_from_settings(s: dict) -> tuple[Options, float | None]:
    """Validate the page's settings JSON. Returns (Options, price per kg)."""
    mode = s.get("size_mode", "height")
    if mode not in ("height", "width", "depth", "scale"):
        raise SettingsError("Unknown size mode")
    value = _num(s, "size_value", 1e-6, None)
    sizes = {"height": None, "width": None, "depth": None, "scale": None}
    if mode == "scale":
        sizes["scale"] = value
    else:
        unit = s.get("size_unit", "mm")
        if unit not in UNITS:
            raise SettingsError("Unknown unit")
        sizes[mode] = parse_length(f"{value}{unit}")

    printer = s.get("printer", DEFAULT_PRINTER)
    if printer == "custom":
        bed = tuple(_num({"bed": v}, "bed", 20, 5000) for v in s.get("bed", []))
        if len(bed) != 3:
            raise SettingsError("Custom bed needs width, depth and height")
    elif printer in PRINTERS:
        bed = tuple(float(v) for v in PRINTERS[printer])
    else:
        raise SettingsError("Unknown printer")

    if s.get("hollow", "auto") not in ("auto", "on", "off"):
        raise SettingsError("Unknown hollow mode")
    if s.get("purpose", "display") not in PURPOSES:
        raise SettingsError("Unknown purpose")
    material = s.get("material", "pla")
    if material not in MATERIAL_DENSITY:
        raise SettingsError("Unknown material")
    if s.get("up", "z") not in ("z", "y", "-y", "x", "-z"):
        raise SettingsError("Unknown up axis")

    price = s.get("price")
    price = _num(s, "price", 0, 10000) if price not in (None, "") else None
    opt = Options(
        bed=bed,  # type: ignore[arg-type]
        margin=_num(s, "margin", 0, 100),
        up=s.get("up", "z"),
        **sizes,
        smooth=bool(s.get("smooth")),
        smooth_angle=_num(s, "smooth_angle", 1, 180),
        smooth_edge=_num(s, "smooth_edge", 0.5, 100),
        optimise_cuts=not s.get("even_cuts"),
        dowels=bool(s.get("dowels", True)),
        dowel=DowelSpec(
            diameter=_num(s, "dowel_d", 1, 50),
            depth=_num(s, "dowel_depth", 3, 200),
            clearance=_num(s, "clearance", 0, 3),
        ),
        labels=bool(s.get("labels", True)),
        repair=bool(s.get("repair", True)),
        merge_small=bool(s.get("merge_small", True)),
        hollow=s.get("hollow", "auto"),
        wall=_num(s, "wall", 0.8, 50),
        purpose=s.get("purpose", "display"),
        nozzle=_num(s, "nozzle", 0.1, 2),
        density=MATERIAL_DENSITY[material],
    )
    return opt, price


def summarize(result: Result, price: float | None) -> dict:
    grams = sum(fp.fill.grams for fp in result.pieces)
    solid = sum(fp.fill.grams_solid for fp in result.pieces)
    return {
        "pieces": len(result.pieces),
        "kg": grams / 1000,
        "solid_kg": solid / 1000,
        "preset": {
            "label": PURPOSES[result.options.purpose].label,
            "walls": PURPOSES[result.options.purpose].walls,
            "infill": PURPOSES[result.options.purpose].infill_pct,
            "pattern": PURPOSES[result.options.purpose].pattern,
            "why": PURPOSES[result.options.purpose].why,
        },
        "basis": result.fill_basis,
        "cost": grams / 1000 * price if price else None,
        "size": [round(v) for v in result.size],
        "scale": result.scale_factor,
        "cuts": result.plan.describe(),
        "dowels": sum(len(j.dowels) for j in result.joints),
        "pins": [{"length": 2 * d, "count": n} for d, n in sorted(result.pin_counts.items())],
        "dropped": result.dropped_cells,
        "warnings": result.warnings,
        "table": [
            {
                "id": fp.id,
                "size": [round(v) for v in fp.placement.size],
                "fill": "solid" if fp.fill.mode == "solid" else f"{fp.fill.mode}: {fp.fill.walls} walls, {fp.fill.infill_pct}",
                "grams": round(fp.fill.grams),
            }
            for fp in result.pieces
        ],
    }


# ---------------------------------------------------------------- worker process
#
# Each preview runs in its own process, which keeps the Result in memory so a
# later save is instant. Cancel kills that process outright, so it stops at
# once even mid-way through a long geometry step, and a crash inside a native
# library takes down only the worker, never the GUI server. A spare worker is
# kept warm (libraries already imported) so the next run starts immediately.


def _worker_main(cmd_q, evt_q) -> None:
    """Child process: run commands from the GUI until told to exit (or killed)."""
    from .export import write_outputs

    # Windows doesn't end child processes with their parent, so exit on our own
    # if the GUI goes away (terminal closed, process killed) instead of lingering.
    parent = mp.parent_process()
    if parent is not None:
        def watchdog() -> None:
            parent.join()
            os._exit(0)

        threading.Thread(target=watchdog, daemon=True).start()

    def log(line: str) -> None:
        evt_q.put(("log", line))

    def progress(fraction: float, stage: str) -> None:
        evt_q.put(("progress", fraction, stage))

    result, price = None, None
    while True:
        cmd = cmd_q.get()
        try:
            if cmd[0] == "preview":
                _, model_path, settings, preview_path = cmd
                opt, price = options_from_settings(settings)
                result = run(model_path, opt, log=log, progress=progress)
                progress(1.0, "Building 3D preview")
                write_preview(result, Path(preview_path))
                evt_q.put(("done", summarize(result, price)))
            elif cmd[0] == "save":
                out = Path(cmd[1])
                write_outputs(result, out, price_per_kg=price, progress=progress)
                log(f"Saved {len(result.pieces)} pieces to {out}")
                evt_q.put(("done", None))
            elif cmd[0] == "exit":
                return
        except Exception as e:
            evt_q.put(("error", _friendly(e), traceback.format_exc()))


class Worker:
    def __init__(self) -> None:
        ctx = mp.get_context("spawn")
        self.cmd_q, self.evt_q = ctx.Queue(), ctx.Queue()
        self.proc = ctx.Process(target=_worker_main, args=(self.cmd_q, self.evt_q), daemon=True)
        self.proc.start()

    def alive(self) -> bool:
        return self.proc.is_alive()

    def send(self, *cmd) -> None:
        self.cmd_q.put(cmd)

    def kill(self) -> None:
        if self.proc.is_alive():
            self.proc.kill()
        self.proc.join(timeout=5)
        for q in (self.cmd_q, self.evt_q):
            q.cancel_join_thread()
            q.close()


@dataclass
class Job:
    kind: str  # "preview" or "save"
    state: str = "running"  # running | done | error | cancelled
    fraction: float = 0.0
    stage: str = "Starting"
    log: list[str] = field(default_factory=list)
    error: str | None = None
    out: str | None = None

    def to_json(self) -> dict:
        return {
            "kind": self.kind, "state": self.state, "fraction": self.fraction, "stage": self.stage,
            "log": self.log[-200:], "error": self.error, "out": self.out,
        }


class Session:
    def __init__(self, warm_spare: bool = True) -> None:
        self.lock = threading.Lock()
        self.workdir = Path(tempfile.mkdtemp(prefix="bigprint-gui-"))
        self.models: dict[str, dict] = {}
        self.job: Job | None = None
        self.worker: Worker | None = None  # holds the current preview's Result
        self.spare: Worker | None = None
        self.warm_spare = warm_spare
        self.result_key: str | None = None  # settings JSON that produced the worker's result
        self.summary: dict | None = None
        self.preview_path = self.workdir / "preview.html"
        self.last_contact = time.monotonic()
        if warm_spare:
            self.spare = Worker()

    def busy(self) -> bool:
        return self.job is not None and self.job.state == "running"

    def _take_worker(self) -> Worker:
        w = self.spare if self.spare is not None and self.spare.alive() else Worker()
        self.spare = Worker() if self.warm_spare else None
        return w

    def _drop_result(self) -> None:
        if self.worker is not None:
            self.worker.kill()
        self.worker, self.result_key, self.summary = None, None, None

    def add_model(self, name: str, data: bytes) -> dict:
        safe = re.sub(r"[^A-Za-z0-9._ -]", "_", Path(name).name) or "model.stl"
        model_id = uuid.uuid4().hex[:12]
        folder = self.workdir / "models" / model_id
        folder.mkdir(parents=True)
        path = folder / safe
        path.write_bytes(data)
        try:
            tm = trimesh.load(str(path), force="mesh")
        except Exception as e:  # trimesh raises many types for bad files
            raise SettingsError(f"Could not read {safe}: {e}")
        if not isinstance(tm, trimesh.Trimesh) or len(tm.faces) == 0:
            raise SettingsError(f"No triangles found in {safe}")
        stem = Path(safe).stem
        docs = Path.home() / "Documents"
        info = {
            "id": model_id,
            "name": safe,
            "extents": [float(v) for v in tm.extents],
            "triangles": int(len(tm.faces)),
            "median_edge": float(np.median(tm.edges_unique_length)),
            "watertight": bool(tm.is_watertight),
            "default_out": str((docs if docs.is_dir() else Path.home()) / "bigprint" / f"{stem}_bigprint"),
        }
        self.models[model_id] = {**info, "path": str(path)}
        return info

    def start_preview(self, settings: dict) -> None:
        """Call with self.lock held."""
        model = self.models.get(settings.get("model_id", ""))
        if model is None:
            raise SettingsError("Choose a model first")
        options_from_settings(settings)  # report bad input now, not from the worker
        key = json.dumps(settings, sort_keys=True)
        self._drop_result()
        worker = self._take_worker()
        job = Job("preview")
        self.job, self.worker = job, worker
        worker.send("preview", model["path"], settings, str(self.preview_path))

        def on_done(summary: dict) -> None:
            self.result_key, self.summary = key, summary

        threading.Thread(target=self._pump, args=(job, worker, on_done), daemon=True).start()

    def start_save(self, settings: dict, out: str) -> None:
        """Call with self.lock held."""
        if self.worker is None or not self.worker.alive() or json.dumps(settings, sort_keys=True) != self.result_key:
            raise SettingsError("Settings changed since the preview. Run Preview again first.")
        if not out.strip():
            raise SettingsError("Choose an output folder")
        out_path = Path(os.path.expandvars(os.path.expanduser(out.strip())))
        if not out_path.is_absolute():
            raise SettingsError("Output folder must be a full path, e.g. C:\\Users\\you\\Documents\\prints")
        job = Job("save", out=str(out_path))
        self.job = job
        self.worker.send("save", str(out_path))
        threading.Thread(target=self._pump, args=(job, self.worker, None), daemon=True).start()

    def cancel(self) -> None:
        """Stop the running job immediately by killing its worker process."""
        with self.lock:
            job = self.job
            if job is None or job.state != "running":
                return
            job.state = "cancelled"
            if job.kind == "save":
                job.log.append("Save cancelled; files already written to the folder may be incomplete.")
            self._drop_result()  # the killed worker held the result
            if self.warm_spare and (self.spare is None or not self.spare.alive()):
                self.spare = Worker()

    def _pump(self, job: Job, worker: Worker, on_done) -> None:
        """Relay events from a worker to its Job until the job ends."""
        while True:
            try:
                ev = worker.evt_q.get(timeout=0.25)
            except queue.Empty:
                with self.lock:
                    if job.state != "running":
                        return
                    if not worker.alive():
                        job.state = "error"
                        job.error = (
                            f"The processing engine stopped unexpectedly (exit code {worker.proc.exitcode}). "
                            "Try again, or try different settings (for example without smoothing)."
                        )
                        if self.worker is worker:
                            self._drop_result()
                        return
                continue
            except (OSError, ValueError, EOFError):  # queue closed by a kill
                return
            with self.lock:
                if job.state != "running":  # cancelled while this event was in flight
                    return
                if ev[0] == "log":
                    job.log.append(ev[1])
                elif ev[0] == "progress":
                    job.fraction, job.stage = ev[1], ev[2]
                elif ev[0] == "error":
                    job.state, job.error = "error", ev[1]
                    job.log.append(ev[2])
                    if job.kind == "preview":
                        self._drop_result()
                    return
                elif ev[0] == "done":
                    if on_done:
                        on_done(ev[1])
                    job.state = "done"
                    return

    def close(self) -> None:
        """Stop all worker processes and delete this session's temp files."""
        with self.lock:
            if self.job is not None and self.job.state == "running":
                self.job.state = "cancelled"
            for w in (self.worker, self.spare):
                if w is not None:
                    w.kill()
            self.worker = self.spare = None
        shutil.rmtree(self.workdir, ignore_errors=True)


def _friendly(e: Exception) -> str:
    if isinstance(e, PermissionError):
        return f"Permission denied: {e.filename}. Choose another output folder."
    return str(e) or e.__class__.__name__


def open_folder(path: str) -> None:
    p = Path(path)
    if not p.is_dir():
        raise SettingsError("That folder doesn't exist yet")
    if sys.platform == "win32":
        os.startfile(str(p))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(p)])
    else:
        subprocess.Popen(["xdg-open", str(p)])


# ---------------------------------------------------------------- HTTP


def make_handler(session: Session, port_ref: list[int], on_quit=None):
    """`on_quit`, if given, is called (from a request thread) when the page asks to quit."""
    page = resources.files("bigprint").joinpath("static/gui.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:  # keep the console quiet
            pass

        # -- helpers
        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").lower()
            return host in (f"127.0.0.1:{port_ref[0]}", f"localhost:{port_ref[0]}")

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200) -> None:
            self._send(code, json.dumps(obj).encode(), "application/json")

        def _error(self, msg: str, code: int = 400) -> None:
            self._json({"error": msg}, code)

        def _body(self) -> bytes:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_UPLOAD:
                raise SettingsError("File is larger than 1 GB")
            return self.rfile.read(n)

        # -- routes
        def do_GET(self) -> None:
            if not self._host_ok():
                return self._error("Forbidden", HTTPStatus.FORBIDDEN)
            session.last_contact = time.monotonic()
            route = urlparse(self.path).path
            if route == "/api/ping":  # page heartbeat; also lets a second launch find us
                return self._json({"app": "bigprint", "ok": True})
            if route == "/":
                return self._send(200, page, "text/html; charset=utf-8")
            if route == "/favicon.ico":
                self.send_response(HTTPStatus.NO_CONTENT)
                self.end_headers()
                return
            if route == "/api/config":
                return self._json({
                    "printers": PRINTERS, "default_printer": DEFAULT_PRINTER,
                    "materials": list(MATERIAL_DENSITY), "purposes": list(PURPOSES), "units": UNITS,
                    "presets": {k: {"label": p.label, "describe": p.describe(), "why": p.why} for k, p in PURPOSES.items()},
                    "project": {"source": SOURCE_URL, "license": LICENSE, "support": SUPPORT_LINKS},
                })
            if route == "/api/job":
                with session.lock:
                    job = session.job.to_json() if session.job else None
                    summary = session.summary
                    return self._json({"job": job, "summary": summary, "result_key": session.result_key})
            if route == "/api/preview":
                if not session.preview_path.exists():
                    return self._error("No preview yet", 404)
                return self._send(200, session.preview_path.read_bytes(), "text/html; charset=utf-8")
            return self._error("Not found", 404)

        def do_POST(self) -> None:
            if not self._host_ok() or self.headers.get("X-Bigprint") != "1":
                return self._error("Forbidden", HTTPStatus.FORBIDDEN)
            session.last_contact = time.monotonic()
            route = urlparse(self.path).path
            try:
                if route == "/api/upload":
                    name = unquote(self.headers.get("X-Filename") or "model.stl")
                    if session.busy():
                        raise SettingsError("Wait for the current job to finish")
                    return self._json(session.add_model(name, self._body()))
                data = json.loads(self._body() or b"{}")
                if route == "/api/preview":
                    with session.lock:
                        if session.busy():
                            raise SettingsError("A job is already running")
                        session.start_preview(data["settings"])
                    return self._json({"ok": True})
                if route == "/api/save":
                    with session.lock:
                        if session.busy():
                            raise SettingsError("A job is already running")
                        session.start_save(data["settings"], data.get("out", ""))
                    return self._json({"ok": True})
                if route == "/api/cancel":
                    session.cancel()
                    return self._json({"ok": True})
                if route == "/api/open-folder":
                    open_folder(data.get("path", ""))
                    return self._json({"ok": True})
                if route == "/api/quit":
                    if on_quit is None:
                        return self._error("Quit is not available here", 404)
                    self._json({"ok": True})
                    self.wfile.flush()
                    on_quit()
                    return
            except SettingsError as e:
                return self._error(str(e))
            except (KeyError, json.JSONDecodeError):
                return self._error("Bad request")
            return self._error("Not found", 404)

    return Handler


def _running_instance(port: int) -> bool:
    """True if a bigprint GUI is already answering on this port."""
    try:
        req = Request(f"http://127.0.0.1:{port}/api/ping", headers={"Host": f"127.0.0.1:{port}"})
        with urlopen(req, timeout=1.5) as r:
            return json.loads(r.read()).get("app") == "bigprint"
    except (OSError, ValueError):
        return False


def serve(port: int = 8765, open_browser: bool = True, idle_minutes: float = IDLE_MINUTES) -> None:
    if _running_instance(port):
        # Launched again (e.g. a second double-click): just reopen the page.
        print(f"bigprint is already running at http://127.0.0.1:{port}/ - opening it.", flush=True)
        if open_browser:
            webbrowser.open(f"http://127.0.0.1:{port}/")
        return

    session = Session()
    port_ref = [port]
    server: ThreadingHTTPServer | None = None

    def quit_from_page() -> None:
        print("Quit requested from the GUI. Shutting down...", flush=True)
        # shutdown() blocks until serve_forever returns, so run it off the request thread.
        threading.Thread(target=server.shutdown, daemon=True).start()

    handler = make_handler(session, port_ref, on_quit=quit_from_page)
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    except OSError:  # port busy: let the OS pick one
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port_ref[0] = server.server_address[1]
    url = f"http://127.0.0.1:{port_ref[0]}/"
    print(f"bigprint GUI running at {url}  (use Quit in the page, or press Ctrl+C here, to stop)", flush=True)
    if open_browser:
        webbrowser.open(url)

    if idle_minutes > 0:
        # With no console window there's no Ctrl+C: if the tab is closed without
        # Quit, stop once no page has been in touch for a while. Never mid-job.
        def watch_idle() -> None:
            while True:
                time.sleep(min(15.0, idle_minutes * 30))
                idle = time.monotonic() - session.last_contact
                if idle > idle_minutes * 60 and not session.busy():
                    print(f"No browser tab for {idle_minutes:g} minutes. Shutting down...", flush=True)
                    server.shutdown()
                    return

        threading.Thread(target=watch_idle, daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        session.close()
        print("bigprint stopped.", flush=True)


def _log_to_file_if_windowless() -> Path | None:
    """A windowed .exe has no console: send output to a log file instead."""
    if sys.stdout is not None:
        return None
    folder = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "bigprint"
    folder.mkdir(parents=True, exist_ok=True)
    log = folder / "bigprint-gui.log"
    if log.exists() and log.stat().st_size > 1_000_000:
        log.replace(folder / "bigprint-gui.old.log")
    stream = open(log, "a", encoding="utf-8", buffering=1)
    stream.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
    sys.stdout = sys.stderr = stream
    return log


def _show_error(message: str) -> None:
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, "bigprint", 0x10)  # MB_ICONERROR


def main() -> None:
    import argparse

    mp.freeze_support()  # lets the packaged .exe start its worker processes

    ap = argparse.ArgumentParser(description="Start the bigprint GUI in your browser.")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true", help="Don't open a browser window")
    ap.add_argument("--idle-minutes", type=float, default=IDLE_MINUTES,
                    help="Quit after this long with no browser tab open (0 = never)")
    log = _log_to_file_if_windowless()
    args = ap.parse_args()
    try:
        serve(args.port, open_browser=not args.no_browser, idle_minutes=args.idle_minutes)
    except Exception:
        traceback.print_exc()
        where = f"\n\nDetails were written to {log}" if log else ""
        _show_error(f"bigprint could not start:\n\n{traceback.format_exc(limit=1)}{where}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()

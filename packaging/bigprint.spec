# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Michael Christopher Stone
# PyInstaller spec: builds dist/bigprint/ with two programs sharing one set of libraries.
#   bigprint.exe      - GUI (no console window); double-click to start
#   bigprint-cli.exe  - command line
# Build with packaging/build_windows.ps1 rather than calling this directly.

import os

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

here = SPECPATH  # noqa: F821 (provided by PyInstaller)

datas = (
    collect_data_files("bigprint")  # GUI page and bundled three.js
    + collect_data_files("trimesh")  # trimesh's JSON resources
)
binaries = collect_dynamic_libs("manifold3d")
hiddenimports = (
    collect_submodules("skimage.measure")  # scikit-image loads submodules lazily
    + collect_submodules("bigprint")
    # trimesh imports these inside try-blocks (3MF support), which the analysis can miss
    + ["scipy.ndimage", "scipy.spatial", "shapely", "manifold3d", "lxml.etree", "networkx"]
)
# Dev/test tools that are installed in the venv but must not be shipped.
excludes = ["matplotlib", "playwright", "pytest", "pyflakes", "tkinter", "IPython", "PyInstaller"]


def analysis(script):
    return Analysis(  # noqa: F821
        [os.path.join(here, script)],
        pathex=[os.path.dirname(here)],
        binaries=binaries,
        datas=datas,
        hiddenimports=hiddenimports,
        excludes=excludes,
        noarchive=False,
    )


gui = analysis("launch_gui.py")
cli = analysis("launch_cli.py")

gui_exe = EXE(  # noqa: F821
    PYZ(gui.pure),  # noqa: F821
    gui.scripts,
    [],
    exclude_binaries=True,
    name="bigprint",
    console=False,
    upx=False,
)
cli_exe = EXE(  # noqa: F821
    PYZ(cli.pure),  # noqa: F821
    cli.scripts,
    [],
    exclude_binaries=True,
    name="bigprint-cli",
    console=True,
    upx=False,
)
COLLECT(  # noqa: F821
    gui_exe, gui.binaries, gui.datas,
    cli_exe, cli.binaries, cli.datas,
    name="bigprint",
    upx=False,
)

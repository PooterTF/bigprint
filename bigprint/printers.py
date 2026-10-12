# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Michael Christopher Stone
"""Build-volume presets (width, depth, height in mm)."""

PRINTERS: dict[str, tuple[int, int, int]] = {
    "bambu-x1": (256, 256, 256),
    "bambu-p1s": (256, 256, 256),
    "bambu-a1": (256, 256, 256),
    "bambu-a1-mini": (180, 180, 180),
    "bambu-h2d": (325, 320, 325),
    "prusa-mk4": (250, 210, 220),
    "prusa-core-one": (250, 220, 270),
    "prusa-xl": (360, 360, 360),
    "prusa-mini": (180, 180, 180),
    "ender3": (220, 220, 250),
    "ender3-v3": (220, 220, 250),
    "creality-k1": (220, 220, 250),
    "creality-k1-max": (300, 300, 300),
    "voron-350": (350, 350, 340),
}

DEFAULT_PRINTER = "bambu-x1"

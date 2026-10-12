# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Michael Christopher Stone
"""Entry point for bigprint.exe (windowed GUI launcher)."""
import multiprocessing

if __name__ == "__main__":
    multiprocessing.freeze_support()  # worker processes re-launch this exe
    from bigprint.gui import main

    main()

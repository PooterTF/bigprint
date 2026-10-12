# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Michael Christopher Stone
"""Entry point for bigprint-cli.exe (command line)."""
import multiprocessing

if __name__ == "__main__":
    multiprocessing.freeze_support()
    from bigprint.cli import app

    app()

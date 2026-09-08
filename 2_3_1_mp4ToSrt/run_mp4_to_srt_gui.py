#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""2_3_1_mp4ToSrt GUI 진입점 (PyInstaller onefile)."""

from __future__ import annotations

import sys
import traceback
from pathlib import Path


def main() -> None:
    _WISDOM = Path(__file__).resolve().parent.parent
    if str(_WISDOM) not in sys.path:
        sys.path.insert(0, str(_WISDOM))
    from wisdom_bootstrap import run as wisdom_run

    wisdom_run(__file__)
    try:
        from mp4_to_srt.gui_app import main as gui_main

        gui_main()
    except Exception:
        if getattr(sys, "frozen", False):
            try:
                import tkinter as tk
                from tkinter import messagebox

                r = tk.Tk()
                r.withdraw()
                messagebox.showerror("2_3_1_mp4ToSrt GUI", traceback.format_exc())
                r.destroy()
            except Exception:
                pass
        else:
            traceback.print_exc()
            raise


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GUI 진입점 — ``dist/2_7_sceneImageScript_gui.exe`` 우선."""

from __future__ import annotations

import os
import subprocess
import sys
import traceback
from pathlib import Path

_MODULE_DIR = Path(__file__).resolve().parent


def _dist_gui_exe() -> Path:
    return _MODULE_DIR / "dist" / "2_7_sceneImageScript_gui.exe"


def main() -> None:
    _WISDOM = _MODULE_DIR.parent
    for p in (_WISDOM, _MODULE_DIR):
        s = str(p)
        if s not in sys.path:
            sys.path.insert(0, s)
    from wisdom_bootstrap import run as wisdom_run

    wisdom_run(__file__)

    from scene_image.settings import set_config_app, set_config_dist

    set_config_app("script")
    if getattr(sys, "frozen", False):
        # exe 안(_MEIPASS)이 아니라 exe 가 있는 dist 에 설정을 저장한다.
        set_config_dist(Path(sys.executable).resolve().parent)
    else:
        set_config_dist(_MODULE_DIR / "dist")

    if getattr(sys, "frozen", False):
        try:
            from scene_image.gui_app import main as gui_main

            gui_main(script_preview=True)
        except Exception:
            _show_error_dialog()
            raise
        return

    exe = _dist_gui_exe()
    use_source = os.environ.get("SCENE_IMAGE_GUI_SOURCE", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )

    if not use_source and exe.is_file():
        r = subprocess.run([str(exe)], cwd=str(_MODULE_DIR))
        raise SystemExit(r.returncode or 0)

    try:
        from scene_image.gui_app import main as gui_main
    except Exception:
        traceback.print_exc()
        raise

    gui_main(script_preview=True)


def _show_error_dialog() -> None:
    try:
        import tkinter as tk
        from tkinter import messagebox

        r = tk.Tk()
        r.withdraw()
        messagebox.showerror("2_7 sceneImageScript", traceback.format_exc())
        r.destroy()
    except Exception:
        pass


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""허브 탭별 GUI 로드 (지연 import)."""

from __future__ import annotations

import importlib
import sys
import traceback
from pathlib import Path
from typing import Callable

import tkinter as tk
from tkinter import scrolledtext, ttk

from wisdom_root import module_dir
from wuxia_hub.pipeline import HUB_TABS

_TAB_MAIN: dict[str, tuple[str, str]] = {
    "1_3_plotToPrompt": ("plot_to_prompt.gui_app", "main"),
    "1_1_volumePlotToPrompt": ("volume_plot.gui_app", "main"),
    "1_5_textToJson": ("text_to_json.gui_app", "main"),
    "2_2_scriptToVoice": ("script_voice.gui_app", "main"),
    "2_3_stt": ("stt.gui_app", "main"),
    "2_3_1_mp4ToSrt": ("mp4_to_srt.gui_app", "main"),
    "2_3_2_mp4VoiceReplace": ("mp4_voice_replace.gui_app", "main"),
    "2_4_srtEdit": ("srt_edit.gui_app", "main"),
    "2_5_sceneImage": ("scene_image.gui_app", "main"),
    "3_2_pngToJpg": ("png2jpg.gui_app", "main"),
    "7_3_mp4Search": ("mp4_search.gui_app", "main"),
    "7_4_mp4Merge": ("mp4_merge.gui_app", "main"),
}


def _ensure_module_path(module: str) -> Path:
    d = module_dir(module)
    s = str(d)
    if s not in sys.path:
        sys.path.insert(0, s)
    return d


def _show_load_error(container: tk.Misc, module: str, tb: str) -> None:
    for w in container.winfo_children():
        w.destroy()
    fr = ttk.Frame(container, padding=12)
    fr.pack(fill=tk.BOTH, expand=True)
    ttk.Label(fr, text=f"{module} GUI 로드 실패", font=("", 11, "bold")).pack(anchor=tk.W)
    txt = scrolledtext.ScrolledText(fr, height=16, wrap=tk.WORD)
    txt.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
    txt.insert("1.0", tb)
    txt.configure(state=tk.DISABLED)


def load_tab_ui(module: str, container: tk.Misc) -> None:
    """모듈 GUI 를 ``container``(탭 Frame) 안에 구성합니다."""
    if getattr(container, "_wuxia_tab_module", None) == module:
        return
    spec = _TAB_MAIN.get(module)
    if spec is None:
        raise ValueError(f"알 수 없는 모듈: {module}")
    mod_name, fn_name = spec
    _ensure_module_path(module)
    try:
        mod = importlib.import_module(mod_name)
        fn = getattr(mod, fn_name)
        fn(container=container)
        setattr(container, "_wuxia_tab_module", module)
    except Exception:
        _show_load_error(container, module, traceback.format_exc())


def tab_loader(module: str) -> Callable[[tk.Misc], None]:
    return lambda c, m=module: load_tab_ui(m, c)


LOADERS: dict[str, Callable[[tk.Misc], None]] = {
    mod: tab_loader(mod) for _title, mod in HUB_TABS
}

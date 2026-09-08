# -*- coding: utf-8 -*-
"""2_3_1_mp4ToSrt GUI 설정 — MP4 → SRT."""

from __future__ import annotations

import json
import importlib.util
import sys
from pathlib import Path

PROJECT_DIRNAME = "2_3_1_mp4ToSrt"
GUI_CONFIG_NAME = "mp4_to_srt_gui_config.json"
MODEL_CHOICES = ("tiny", "base", "small", "medium", "large-v3")
_VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi"}


def _ensure_wisdom_on_path(from_file: str | Path) -> None:
    if importlib.util.find_spec("wisdom_root") is not None:
        return
    candidates: list[Path] = [Path.cwd(), *Path(from_file).resolve().parents]
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        candidates.append(Path(meipass))
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent)
    seen: set[str] = set()
    for base in candidates:
        try:
            root = base.resolve()
        except OSError:
            continue
        key = str(root)
        if key in seen:
            continue
        seen.add(key)
        if (root / "wisdom_root.py").is_file():
            if key not in sys.path:
                sys.path.insert(0, key)
            return


_ensure_wisdom_on_path(__file__)
from wisdom_root import module_dir
from wisdom_workspace import (
    folder_dialog_initial,
    get_workspace_dir,
    resolve_module_output,
    touch_workspace_from_path,
)


def _frozen_exe_dir() -> Path:
    return Path(sys.executable).resolve().parent


def module_dist_dir() -> Path:
    return module_dir(PROJECT_DIRNAME) / "dist"


def default_output_dir() -> Path:
    ws = get_workspace_dir()
    if ws is not None:
        d = ws / PROJECT_DIRNAME / "output"
        d.mkdir(parents=True, exist_ok=True)
        return d
    return resolve_module_output(PROJECT_DIRNAME)


def list_video_files(folder: Path | str) -> list[Path]:
    d = Path(folder)
    if not d.is_dir():
        return []
    found: list[Path] = []
    try:
        for p in d.iterdir():
            if p.is_file() and p.suffix.lower() in _VIDEO_EXTS:
                found.append(p)
    except OSError:
        return []
    return sorted(found, key=lambda x: x.stat().st_mtime, reverse=True)


def gui_config_path() -> Path:
    if getattr(sys, "frozen", False):
        return _frozen_exe_dir() / GUI_CONFIG_NAME
    d = module_dist_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d / GUI_CONFIG_NAME


def load_gui_settings() -> dict[str, str]:
    p = gui_config_path()
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, str] = {}
    for key in (
        "mp4_path",
        "mp4_folder",
        "output_dir",
        "whisper_model",
        "language",
        "srt_beside",
    ):
        v = data.get(key)
        if isinstance(v, str) and v.strip():
            out[key] = v.strip()
        elif key == "srt_beside" and isinstance(v, bool):
            out[key] = "1" if v else "0"
    return out


def save_gui_settings(
    *,
    mp4_path: str = "",
    mp4_folder: str = "",
    output_dir: str = "",
    whisper_model: str = "base",
    language: str = "ko",
    srt_beside: bool = True,
) -> None:
    for raw in (mp4_path, mp4_folder, output_dir):
        if raw.strip():
            touch_workspace_from_path(raw)
            break
    p = gui_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "mp4_path": mp4_path.strip(),
        "mp4_folder": mp4_folder.strip(),
        "output_dir": output_dir.strip(),
        "whisper_model": whisper_model.strip() or "base",
        "language": language.strip() or "ko",
        "srt_beside": "1" if srt_beside else "0",
    }
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


__all__ = [
    "MODEL_CHOICES",
    "PROJECT_DIRNAME",
    "default_output_dir",
    "folder_dialog_initial",
    "list_video_files",
    "load_gui_settings",
    "module_dist_dir",
    "save_gui_settings",
]

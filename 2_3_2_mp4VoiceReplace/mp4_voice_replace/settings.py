# -*- coding: utf-8 -*-
"""2_3_2_mp4VoiceReplace GUI 설정."""

from __future__ import annotations

import json
import importlib.util
import sys
from pathlib import Path

PROJECT_DIRNAME = "2_3_2_mp4VoiceReplace"
GUI_CONFIG_NAME = "mp4_voice_replace_gui_config.json"


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


def guess_srt_beside_mp4(mp4: Path) -> Path | None:
    p = mp4.with_suffix(".srt")
    return p if p.is_file() else None


def guess_mp3_dir(mp4: Path) -> Path | None:
    """``…/mp4/foo.mp4`` → ``…/mp3`` if lines.json exists."""
    parent = mp4.parent
    if parent.name.casefold() == "mp4":
        cand = parent.parent / "mp3"
        if (cand / "lines.json").is_file():
            return cand
    sib = parent / "mp3"
    if (sib / "lines.json").is_file():
        return sib
    return None


def default_dest_for_mp4(mp4: Path, out_dir: Path | None = None) -> Path:
    name = f"{mp4.stem}_voice.mp4"
    if out_dir is not None:
        return Path(out_dir) / name
    return mp4.with_name(name)


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
    for key in ("mp4_path", "srt_path", "mp3_dir", "output_path"):
        v = data.get(key)
        if isinstance(v, str) and v.strip():
            out[key] = v.strip()
    return out


def save_gui_settings(
    *,
    mp4_path: str = "",
    srt_path: str = "",
    mp3_dir: str = "",
    output_path: str = "",
) -> None:
    for raw in (mp4_path, srt_path, mp3_dir, output_path):
        if raw.strip():
            touch_workspace_from_path(raw)
            break
    p = gui_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "mp4_path": mp4_path.strip(),
        "srt_path": srt_path.strip(),
        "mp3_dir": mp3_dir.strip(),
        "output_path": output_path.strip(),
    }
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


__all__ = [
    "PROJECT_DIRNAME",
    "default_dest_for_mp4",
    "default_output_dir",
    "folder_dialog_initial",
    "guess_mp3_dir",
    "guess_srt_beside_mp4",
    "load_gui_settings",
    "save_gui_settings",
]

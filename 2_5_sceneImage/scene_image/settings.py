# -*- coding: utf-8 -*-
"""GUI 설정 저장."""

from __future__ import annotations

import json
import sys
from pathlib import Path

CONFIG_NAME = "scene_image_gui_config.json"
CONFIG_NAME_SCRIPT = "scene_image_script_gui_config.json"

_KEYS = (
    "root_dir",
    "png_dir",
    "tts_dir",
    "genspark_url",
    "genspark_model_selector",
    "scene_script",
    "scene_index",
    "srt_path",
    "prompt_path",
    "hourly_limit_retry",
    "limit_session_start",
    "shutdown_after_complete",
    "shutdown_after_hours",
    "manual_secs",
    "prev_image_reference",
    "single_sec",
    "single_prompt",
    "preview_image_scale",
)

# 슬롯별 설정 파일 분리 (동시 인스턴스가 루트/png 를 덮어쓰지 않게)
_config_slot_index: int | None = None
_config_app: str = "scene"
_config_dist: Path | None = None


def set_config_slot(index: int | None) -> None:
    global _config_slot_index
    _config_slot_index = None if index is None else int(index)


def set_config_app(app_id: str) -> None:
    """``scene`` (기본) · ``script`` (2_7_sceneImageScript)."""
    global _config_app
    _config_app = (app_id or "scene").strip().lower() or "scene"


def set_config_dist(dist_dir: Path | str | None) -> None:
    global _config_dist
    _config_dist = Path(dist_dir) if dist_dir else None


def config_path() -> Path:
    if _config_dist is not None:
        base = _config_dist
    elif getattr(sys, "frozen", False):
        base = Path(sys.executable).resolve().parent
    else:
        base = Path(__file__).resolve().parents[1] / "dist"
    base_name = CONFIG_NAME_SCRIPT if _config_app == "script" else CONFIG_NAME
    if _config_slot_index is None or _config_slot_index == 0:
        name = base_name
    else:
        stem = base_name.removesuffix(".json")
        name = f"{stem}_slot{_config_slot_index}.json"
    return base / name


def load_gui_settings() -> dict[str, str]:
    p = config_path()
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, str] = {}
    for k in _KEYS:
        v = data.get(k)
        if isinstance(v, str):
            out[k] = v
        elif k in ("scene_index",) and isinstance(
            v, int
        ):
            out[k] = str(v)
    return out


def load_model_selector() -> str:
    return load_gui_settings().get("genspark_model_selector", "").strip()


def save_gui_settings(**kwargs: str) -> None:
    try:
        from wisdom_workspace import touch_workspace_from_path

        v = kwargs.get("root_dir", "").strip() or kwargs.get("png_dir", "").strip()
        if v:
            touch_workspace_from_path(v)
    except ImportError:
        pass
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    base: dict = {}
    if p.is_file():
        try:
            cur = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(cur, dict):
                base = cur
        except (OSError, json.JSONDecodeError, ValueError):
            base = {}
    for k in _KEYS:
        if k in kwargs and kwargs[k] is not None:
            base[k] = str(kwargs[k])
    p.write_text(json.dumps(base, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

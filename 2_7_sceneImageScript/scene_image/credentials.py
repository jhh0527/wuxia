# -*- coding: utf-8 -*-
"""Genspark 로그인 계정 — 모듈 dist 공통 JSON (슬롯·허브·exe 경로 무관)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

CRED_NAME = "scene_image_credentials.json"
# 다른 모듈·이전 저장 위치 폴백
_FALLBACK_NAMES = (
    CRED_NAME,
    "srt_edit_credentials.json",
)


def _module_dist_dir() -> Path:
    """2_5_sceneImage/dist — 모든 인스턴스가 읽·쓰는 공통 위치."""
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).resolve()
        if exe.name.casefold() == "2_5_sceneimage_gui.exe":
            return exe.parent
    try:
        from wisdom_root import module_dir

        return module_dir("2_5_sceneImage") / "dist"
    except Exception:
        pass
    return Path(__file__).resolve().parents[1] / "dist"


def credentials_path() -> Path:
    """저장·우선 로드 경로 (슬롯·포트와 무관)."""
    return _module_dist_dir() / CRED_NAME


def _candidate_paths() -> list[Path]:
    out: list[Path] = []
    seen: set[str] = set()

    def add(p: Path) -> None:
        key = str(p.resolve()) if p.exists() else str(p)
        if key not in seen:
            seen.add(key)
            out.append(p)

    add(credentials_path())
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).resolve().parent
        for name in _FALLBACK_NAMES:
            add(base / name)
    here = Path(__file__).resolve().parents[1]
    wisdom = here.parent
    for name in _FALLBACK_NAMES:
        add(here / "dist" / name)
    add(wisdom / "2_5_sceneImage" / "dist" / CRED_NAME)
    add(wisdom / "2_4_srtEdit" / "dist" / "srt_edit_credentials.json")
    add(wisdom / "dist" / CRED_NAME)
    add(wisdom / "dist" / "srt_edit_credentials.json")
    return out


def load_credentials() -> tuple[str, str]:
    """(email, password). 환경변수 SCENE_IMAGE_EMAIL / SCENE_IMAGE_PASSWORD 우선."""
    email = os.environ.get("SCENE_IMAGE_EMAIL", "").strip()
    password = os.environ.get("SCENE_IMAGE_PASSWORD", "").strip()
    if email and password:
        return email, password
    for p in _candidate_paths():
        if not p.is_file():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        e = str(data.get("email") or data.get("id") or "").strip()
        pw = str(data.get("password") or data.get("pw") or "").strip()
        if e and pw:
            return e, pw
    return "", ""


def save_credentials(email: str, password: str) -> None:
    """공통 dist 경로에 저장 — 새 슬롯·인스턴스도 동일 계정 사용."""
    p = credentials_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(
            {"email": email.strip(), "password": password},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

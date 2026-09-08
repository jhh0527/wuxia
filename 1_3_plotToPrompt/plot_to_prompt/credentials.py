# -*- coding: utf-8 -*-
"""Genspark 로그인 계정 — dist 로컬 JSON (소스·커밋에 비밀번호 넣지 않음)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

CRED_NAME = "plot_to_prompt_credentials.json"
_FALLBACK_NAMES = (
    CRED_NAME,
    "text_to_json_credentials.json",
    "srt_edit_credentials.json",
    "scene_image_credentials.json",
)


def credentials_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / CRED_NAME
    return Path(__file__).resolve().parents[1] / "dist" / CRED_NAME


def _candidate_paths() -> list[Path]:
    out: list[Path] = []
    seen: set[str] = set()

    def add(p: Path) -> None:
        key = str(p)
        if key not in seen:
            seen.add(key)
            out.append(p)

    add(credentials_path())
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).resolve().parent
        for name in _FALLBACK_NAMES:
            add(base / name)
    else:
        here = Path(__file__).resolve().parents[1]
        wisdom = here.parent
        for name in _FALLBACK_NAMES:
            add(here / "dist" / name)
        add(wisdom / "1_5_textToJson" / "dist" / "text_to_json_credentials.json")
        add(wisdom / "2_4_srtEdit" / "dist" / "srt_edit_credentials.json")
        add(wisdom / "2_5_sceneImage" / "dist" / "scene_image_credentials.json")
    return out


def load_credentials() -> tuple[str, str]:
    """(email, password). 환경변수 우선. 1_5·2_4·2_5 계정 폴백."""
    email = (
        os.environ.get("PLOT_TO_PROMPT_EMAIL", "").strip()
        or os.environ.get("TEXT_TO_JSON_EMAIL", "").strip()
        or os.environ.get("SRT_EDIT_EMAIL", "").strip()
        or os.environ.get("SCENE_IMAGE_EMAIL", "").strip()
    )
    password = (
        os.environ.get("PLOT_TO_PROMPT_PASSWORD", "").strip()
        or os.environ.get("TEXT_TO_JSON_PASSWORD", "").strip()
        or os.environ.get("SRT_EDIT_PASSWORD", "").strip()
        or os.environ.get("SCENE_IMAGE_PASSWORD", "").strip()
    )
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

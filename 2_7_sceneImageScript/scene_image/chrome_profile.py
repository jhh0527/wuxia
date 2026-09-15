# -*- coding: utf-8 -*-
"""로컬 Chrome 프로필에서 이메일 계정 매칭."""

from __future__ import annotations

import json
import os
from pathlib import Path


def chrome_user_data_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA", "")
    return Path(local) / "Google" / "Chrome" / "User Data"


def list_chrome_profiles() -> list[tuple[str, str, str]]:
    """[(profile_directory, name, email), ...]"""
    root = chrome_user_data_dir()
    state = root / "Local State"
    if not state.is_file():
        return []
    try:
        data = json.loads(state.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return []
    info = (data.get("profile") or {}).get("info_cache") or {}
    if not isinstance(info, dict):
        return []
    out: list[tuple[str, str, str]] = []
    for key, meta in info.items():
        if not isinstance(meta, dict):
            continue
        name = str(meta.get("name") or key)
        email = str(meta.get("user_name") or meta.get("gaia_name") or "").strip()
        out.append((str(key), name, email))
    return out


def find_profile_directory_for_email(email: str) -> str | None:
    """이메일과 일치하는 Chrome ``--profile-directory`` 이름 (예: Default)."""
    want = (email or "").strip().lower()
    if not want:
        return None
    for directory, _name, user_email in list_chrome_profiles():
        if (user_email or "").strip().lower() == want:
            return directory
    # 부분 일치 (표시명에 이메일 일부)
    for directory, name, user_email in list_chrome_profiles():
        blob = f"{user_email} {name}".lower()
        if want in blob:
            return directory
    return None

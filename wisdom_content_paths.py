# -*- coding: utf-8 -*-
"""콘텐츠 프로젝트 폴더 — 작업 폴더(루트) 아래 mp3 / png / jpg."""

from __future__ import annotations

from pathlib import Path

from wisdom_workspace import get_workspace_dir, set_workspace_dir

_MEDIA_CHILD_NAMES = frozenset({"mp3", "png", "jpg", "mp4"})


def find_child_dir(root: Path, name: str) -> Path:
    """``root`` 아래 자식 폴더 (대소문자 무시). 없으면 ``root/name``."""
    try:
        base = root.expanduser().resolve()
    except OSError:
        return Path(name)
    if not base.is_dir():
        return base / name
    target = name.casefold()
    for child in base.iterdir():
        if child.is_dir() and child.name.casefold() == target:
            return child
    return base / name


def _dir_has_media_files(folder: Path) -> bool:
    if not folder.is_dir():
        return False
    try:
        for entry in folder.iterdir():
            if not entry.is_file():
                continue
            ext = entry.suffix.casefold()
            if ext in (".mp3", ".srt"):
                return True
    except OSError:
        return False
    return False


def infer_content_root(path: str | Path) -> Path:
    """선택 경로에서 콘텐츠 루트(프로젝트 폴더)를 추론합니다."""
    raw = Path(path).expanduser()
    try:
        p = raw.resolve()
    except OSError:
        p = raw

    if p.is_file() or (not p.is_dir() and p.suffix):
        p = p.parent

    if not p.is_dir():
        if p.name.casefold() in _MEDIA_CHILD_NAMES:
            return p.parent
        parent = p.parent
        return parent if parent.is_dir() else p

    if p.name.casefold() in _MEDIA_CHILD_NAMES:
        return p.parent

    if _dir_has_media_files(p):
        return p.parent

    for name in _MEDIA_CHILD_NAMES:
        if find_child_dir(p, name).is_dir():
            return p

    return p.parent


def touch_content_root_from_path(path: str | Path) -> Path | None:
    """선택 경로에서 콘텐츠 루트를 작업 폴더로 저장."""
    root = infer_content_root(path)
    if not root.is_dir():
        return None
    try:
        return set_workspace_dir(root)
    except OSError:
        return root if root.is_dir() else None


def content_root() -> Path | None:
    ws = get_workspace_dir()
    if ws is None:
        return None
    try:
        r = ws.expanduser().resolve()
    except OSError:
        return None
    return r if r.is_dir() else None


def default_mp3_dir() -> Path | None:
    root = content_root()
    if root is None:
        return None
    return find_child_dir(root, "mp3")


def default_png_dir() -> Path | None:
    root = content_root()
    if root is None:
        return None
    return find_child_dir(root, "png")


def default_jpg_dir() -> Path | None:
    root = content_root()
    if root is None:
        return None
    return find_child_dir(root, "jpg")


def default_mp4_dir() -> Path | None:
    root = content_root()
    if root is None:
        return None
    return find_child_dir(root, "mp4")


def ensure_content_dirs(root: Path | str, *names: str) -> dict[str, Path]:
    """콘텐츠 루트 아래 지정한 하위 폴더만 확보 (없으면 생성)."""
    r = Path(root).expanduser()
    r.mkdir(parents=True, exist_ok=True)
    out: dict[str, Path] = {}
    for name in names:
        key = str(name).strip()
        if not key:
            continue
        p = find_child_dir(r, key)
        p.mkdir(parents=True, exist_ok=True)
        out[key] = p
    return out


def ensure_content_layout(root: Path | str) -> dict[str, Path]:
    """콘텐츠 루트 아래 ``tts``/``stt``/``md``/``png``/``jpg``/``mp3``/``mp4`` 확보."""
    return ensure_content_dirs(
        root, "tts", "stt", "md", "png", "jpg", "mp3", "mp4"
    )


def infer_root_from_media_path(path: str | Path) -> Path | None:
    """``…/png``·``…/mp3`` 등 미디어 하위면 부모(콘텐츠 루트), 아니면 해당 폴더."""
    p = Path(path).expanduser()
    try:
        p = p.resolve()
    except OSError:
        return None
    if p.is_file():
        p = p.parent
    if not p.is_dir():
        return None
    if p.name.casefold() in _MEDIA_CHILD_NAMES | frozenset({"mp4", "tts", "stt", "md"}):
        return p.parent
    return p

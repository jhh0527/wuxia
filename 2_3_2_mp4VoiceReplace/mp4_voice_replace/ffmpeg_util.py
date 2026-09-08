# -*- coding: utf-8 -*-
"""ffmpeg / ffprobe 유틸."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

_WIN_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _win_flags() -> dict:
    if sys.platform == "win32" and _WIN_NO_WINDOW:
        return {"creationflags": _WIN_NO_WINDOW}
    return {}


def _tool_bases() -> list[Path]:
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).resolve()
        return [exe.parent, exe.parent.parent, exe.parent.parent.parent]
    here = Path(__file__).resolve()
    return [here.parents[1], here.parents[2]]


def _find_tool(name: str) -> Path | None:
    exe = f"{name}.exe" if sys.platform == "win32" else name
    for base in _tool_bases():
        for rel in (
            Path("tools") / "ffmpeg" / "bin" / exe,
            Path("tools") / "ffmpeg" / exe,
            Path("tools") / exe,
        ):
            p = base / rel
            if p.is_file():
                return p
    w = shutil.which(name)
    return Path(w) if w else None


def ffmpeg_bin() -> Path | None:
    return _find_tool("ffmpeg")


def ffprobe_bin() -> Path | None:
    return _find_tool("ffprobe")


def probe_duration_sec(path: Path | str) -> float | None:
    fp = ffprobe_bin()
    if not fp:
        return None
    cmd = [
        str(fp),
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            **_win_flags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    try:
        dur = float((r.stdout or "").strip())
        return dur if dur > 0 else None
    except ValueError:
        return None


def run_ffmpeg(cmd: list[str], *, timeout: float | None = None) -> None:
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            **_win_flags(),
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError("ffmpeg 시간 초과") from e
    except OSError as e:
        raise RuntimeError(f"ffmpeg 실행 실패: {e}") from e
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip()
        raise RuntimeError(err[-800:] if err else f"ffmpeg 실패 (code {r.returncode})")

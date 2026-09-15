# -*- coding: utf-8 -*-
"""타임라인 합성 구간 자막 — 하단 중앙, 검은 글자 + 흰 테두리."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

COMPOSE_SUBTITLE_FONT_FILE = "GmarketSansTTFBold.ttf"
# libass: "Gmarket Sans TTF" → 맑은고딕 fallback. "Gmarket Sans TTF Bold" → fontsdir TTF 사용.
COMPOSE_SUBTITLE_FONT_NAME = "Gmarket Sans TTF Bold"
COMPOSE_SUBTITLE_FONT_SIZE = 18
COMPOSE_SUBTITLE_MARGIN_V = 18
COMPOSE_SUBTITLE_OUTLINE = 1
COMPOSE_SUBTITLE_FORCE_STYLE = (
    f"FontName={COMPOSE_SUBTITLE_FONT_NAME},FontSize={COMPOSE_SUBTITLE_FONT_SIZE},Bold=0,"
    "PrimaryColour=&H00000000,OutlineColour=&H00FFFFFF,"
    f"BorderStyle=1,Outline={COMPOSE_SUBTITLE_OUTLINE},Shadow=0,"
    f"MarginV={COMPOSE_SUBTITLE_MARGIN_V},Alignment=2"
)

_MAX_CHARS_PER_LINE_IN_CUE = 44


def compose_subtitle_fonts_dir() -> Path | None:
    try:
        from wisdom_root import resolve_wisdom_root

        d = resolve_wisdom_root() / "fonts"
        if (d / COMPOSE_SUBTITLE_FONT_FILE).is_file():
            return d.resolve()
    except (ImportError, OSError):
        pass
    return None


def stage_compose_font_for_work(work_dir: Path) -> Path | None:
    src_root = compose_subtitle_fonts_dir()
    if src_root is None:
        return None
    src = src_root / COMPOSE_SUBTITLE_FONT_FILE
    dest_dir = work_dir.resolve() / "fonts"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / COMPOSE_SUBTITLE_FONT_FILE
    try:
        if not dest.is_file() or dest.stat().st_mtime < src.stat().st_mtime:
            shutil.copy2(src, dest)
    except OSError:
        return None
    return dest_dir


def seconds_to_srt_ts(sec: float) -> str:
    if sec < 0:
        sec = 0.0
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    ms = int(round((s - int(s)) * 1000))
    si = int(s)
    if ms >= 1000:
        si += 1
        ms = 0
    return f"{h:02d}:{m:02d}:{si:02d},{ms:03d}"


def _wrap_cue_lines(text: str, line_len: int = _MAX_CHARS_PER_LINE_IN_CUE) -> str:
    t = text.replace("\r\n", "\n").strip().replace("\n", " ")
    if len(t) <= line_len:
        return t or " "
    lines: list[str] = []
    i = 0
    while i < len(t):
        chunk_end = min(i + line_len, len(t))
        chunk = t[i:chunk_end]
        if chunk_end < len(t) and " " in chunk[:-6]:
            cut = chunk.rfind(" ")
            if cut >= line_len // 2:
                chunk = chunk[:cut]
                i += cut + 1
                lines.append(chunk.strip())
                continue
        lines.append(chunk.strip())
        i = chunk_end
    return "\n".join(x for x in lines if x) or t or " "


def write_timeline_segment_srt(
    dest: Path,
    srt_path: Path,
    start_sec: float,
    duration_sec: float,
) -> bool:
    """타임라인 구간 [start_sec, start_sec+duration) 에 겹치는 SRT 큐를 상대 시각으로 저장."""
    from mp4_search.srt_parse import parse_srt_cues_timed

    dest = Path(dest)
    srt_path = Path(srt_path)
    if not srt_path.is_file():
        return False
    dur = max(0.1, float(duration_sec))
    seg_start = max(0.0, float(start_sec))
    seg_end = seg_start + dur
    body: list[str] = []
    idx = 1
    for _sid, text, st_ms, en_ms in parse_srt_cues_timed(srt_path):
        t = (text or "").strip().replace("\r\n", "\n")
        if not t:
            continue
        st = st_ms / 1000.0
        en = en_ms / 1000.0
        if en <= seg_start + 0.001 or st >= seg_end - 0.001:
            continue
        rel_st = max(0.0, st - seg_start)
        rel_en = min(dur, en - seg_start)
        if rel_en <= rel_st + 0.02:
            rel_en = min(dur, rel_st + 0.12)
        body.extend(
            [
                str(idx),
                f"{seconds_to_srt_ts(rel_st)} --> {seconds_to_srt_ts(rel_en)}",
                _wrap_cue_lines(t),
                "",
            ]
        )
        idx += 1
    if not body:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("\n".join(body), encoding="utf-8")
    return True


def _escape_ffmpeg_force_style(style: str) -> str:
    return style.replace("\\", r"\\").replace(",", r"\,").replace("'", r"\'")


def _fontsdir_filter_opt(ffmpeg_cwd: Path | None) -> str:
    if ffmpeg_cwd is None:
        return ""
    cwd = ffmpeg_cwd.resolve()
    for fonts_dir in (
        cwd / "fonts",
        cwd / "_compose_work" / "fonts",
    ):
        if (fonts_dir / COMPOSE_SUBTITLE_FONT_FILE).is_file():
            try:
                rel = fonts_dir.relative_to(cwd).as_posix()
            except ValueError:
                continue
            return f":fontsdir={rel}"
    return ""


def subtitle_path_filter_arg(
    srt: Path,
    *,
    ffmpeg_cwd: Path | None = None,
    play_res: tuple[int, int] = (1920, 1080),
) -> str:
    """FFmpeg ``subtitles=`` 필터 (SRT + force_style — 크기·위치 고정)."""
    s_abs = srt.resolve()
    pw, ph = play_res
    fs = _escape_ffmpeg_force_style(COMPOSE_SUBTITLE_FORCE_STYLE)
    fonts_opt = _fontsdir_filter_opt(ffmpeg_cwd)
    ch = f"charenc=UTF-8:original_size={pw}x{ph}:force_style='{fs}'{fonts_opt}"

    def _with_filename(path_for_filter: str) -> str:
        if "'" in path_for_filter or ":" in path_for_filter or "," in path_for_filter or " " in path_for_filter:
            esc = path_for_filter.replace("\\", r"\\").replace(":", r"\:").replace("'", r"\'")
            return f"subtitles=filename='{esc}':{ch}"
        return f"subtitles=filename={path_for_filter}:{ch}"

    if ffmpeg_cwd is not None:
        cwd_r = ffmpeg_cwd.resolve()
        try:
            if s_abs.parent == cwd_r or os.path.samefile(s_abs.parent, cwd_r):
                return _with_filename(s_abs.name)
        except (FileNotFoundError, OSError):
            pass
        try:
            rel = s_abs.relative_to(cwd_r).as_posix()
            return _with_filename(rel)
        except ValueError:
            pass

    p = s_abs.as_posix().replace("\\", "/")
    if len(p) >= 2 and p[1] == ":" and p[0].isalpha():
        esc = f"{p[0]}\\:{p[2:]}"
        return f"subtitles=filename={esc}:{ch}"
    return _with_filename(p)

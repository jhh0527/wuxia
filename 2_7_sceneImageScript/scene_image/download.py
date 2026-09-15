# -*- coding: utf-8 -*-
"""이미지 URL → png/SRT_XXX.png 저장."""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from pathlib import Path

from scene_image.scene_parse import srt_png_name
from scene_image.url_filter import is_image_bytes, is_tracking_url, looks_like_image_url

_SRT_IN_URL = re.compile(r"SRT[_\-/]?(\d{1,6})", re.IGNORECASE)


def guess_srt_sec_from_url(url: str) -> int | None:
    m = _SRT_IN_URL.search(url or "")
    return int(m.group(1)) if m else None


def download_url(url: str, dest: Path, *, timeout: float = 90) -> Path:
    url = (url or "").strip()
    if is_tracking_url(url):
        raise RuntimeError(f"추적 URL은 이미지가 아닙니다: {url[:80]}…")
    if not looks_like_image_url(url):
        raise RuntimeError(f"이미지 URL이 아닙니다: {url[:80]}…")
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Referer": "https://www.genspark.ai/",
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
    if not data:
        raise RuntimeError(f"빈 응답: {url[:120]}…")
    if not is_image_bytes(data):
        raise RuntimeError(f"이미지 파일이 아닙니다: {url[:120]}…")
    dest.write_bytes(data)
    return dest


def assign_srt_secs(
    items: list[tuple[int | None, str]],
    *,
    fallback_secs: list[int] | None = None,
    default_start_sec: int | None = None,
) -> list[tuple[int, str]]:
    fb = list(fallback_secs or [])
    fb_i = 0
    auto_i = default_start_sec if default_start_sec is not None else 0
    used: set[int] = set()
    out: list[tuple[int, str]] = []
    for sec, url in items:
        url = (url or "").strip()
        if not url:
            continue
        n = sec
        if n is None:
            n = guess_srt_sec_from_url(url)
        if n is None and fb_i < len(fb):
            n = fb[fb_i]
            fb_i += 1
        if n is None:
            while auto_i in used:
                auto_i += 1
            n = auto_i
            auto_i += 1
        while n in used:
            n += 1
        used.add(int(n))
        out.append((int(n), url))
    return out


def save_images(
    items: list[tuple[int | None, str]],
    png_dir: Path,
    *,
    fallback_secs: list[int] | None = None,
    default_start_sec: int | None = None,
) -> list[Path]:
    png_dir = Path(png_dir)
    png_dir.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    for n, url in assign_srt_secs(
        items,
        fallback_secs=fallback_secs,
        default_start_sec=default_start_sec,
    ):
        dest = png_dir / srt_png_name(n)
        try:
            download_url(url, dest)
            saved.append(dest)
        except (OSError, urllib.error.URLError, TimeoutError, RuntimeError) as e:
            raise RuntimeError(f"{dest.name} 저장 실패: {e}") from e
    return saved

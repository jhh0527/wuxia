# -*- coding: utf-8 -*-
"""PNG → JPEG 변환 및 유튜브·영상 파이프라인용 해상도·용량 최적화."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from png2jpg.naming import extract_srt_number, srt_jpg_name

PNG_EXTS = frozenset({".png", ".PNG"})
JPG_EXTS = frozenset({".jpg", ".jpeg", ".JPG", ".JPEG"})

DEFAULT_MAX_WIDTH = 1920
DEFAULT_MAX_HEIGHT = 1080
DEFAULT_JPEG_QUALITY = 88


@dataclass
class ConvertResult:
    source: Path
    output: Path
    srt_number: int
    bytes_before: int
    bytes_after: int
    size_px: tuple[int, int]
    match_note: str = ""

    @property
    def saved_bytes(self) -> int:
        return max(0, self.bytes_before - self.bytes_after)


@dataclass
class ConvertSkip:
    source: Path
    reason: str


def _fit_size(w: int, h: int, max_w: int, max_h: int) -> tuple[int, int]:
    if w <= max_w and h <= max_h:
        return w, h
    scale = min(max_w / w, max_h / h)
    nw = max(1, int(w * scale))
    nh = max(1, int(h * scale))
    return nw, nh


def _open_rgb(img: Image.Image) -> Image.Image:
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        base = Image.new("RGB", img.size, (255, 255, 255))
        rgba = img.convert("RGBA")
        base.paste(rgba, mask=rgba.split()[-1])
        return base
    if img.mode != "RGB":
        return img.convert("RGB")
    return img


def convert_one(
    src: Path,
    dst: Path,
    *,
    max_width: int = DEFAULT_MAX_WIDTH,
    max_height: int = DEFAULT_MAX_HEIGHT,
    quality: int = DEFAULT_JPEG_QUALITY,
) -> ConvertResult:
    if not src.is_file():
        raise FileNotFoundError(src)
    bytes_before = src.stat().st_size

    with Image.open(src) as im:
        im = _open_rgb(im)
        nw, nh = _fit_size(im.width, im.height, max_width, max_height)
        if (nw, nh) != (im.width, im.height):
            im = im.resize((nw, nh), Image.Resampling.LANCZOS)

        dst.parent.mkdir(parents=True, exist_ok=True)
        im.save(
            dst,
            format="JPEG",
            quality=quality,
            optimize=True,
            progressive=True,
            subsampling=2,
        )

    return ConvertResult(
        source=src.resolve(),
        output=dst.resolve(),
        srt_number=0,
        bytes_before=bytes_before,
        bytes_after=dst.stat().st_size,
        size_px=(nw, nh),
    )


def iter_source_images(
    input_dir: Path,
    *,
    recursive: bool = False,
    include_jpg: bool = False,
) -> list[Path]:
    d = input_dir.resolve()
    if not d.is_dir():
        raise NotADirectoryError(d)

    exts = set(PNG_EXTS)
    if include_jpg:
        exts |= JPG_EXTS

    if recursive:
        files = [p for p in d.rglob("*") if p.is_file() and p.suffix in exts]
    else:
        files = [p for p in d.iterdir() if p.is_file() and p.suffix in exts]
    return sorted(files, key=lambda p: p.name.lower())


def _plan_output_numbers(sources: list[Path]) -> list[tuple[Path, int | None, str]]:
    """각 소스 → (경로, SRT번호, 설명). 파일명 규칙만 사용 (타임스탬프 매칭 없음)."""
    used: set[int] = set()
    planned: list[tuple[Path, int | None, str]] = []

    for src in sorted(sources, key=lambda p: p.name.lower()):
        n = extract_srt_number(src.stem)
        if n is None:
            planned.append((src, None, "파일명에서 번호를 찾을 수 없음"))
            continue
        if n in used:
            planned.append((src, None, f"SRT_{n:03d} 번호 중복"))
            continue
        used.add(n)
        planned.append((src, n, "파일명 번호"))

    return planned


def convert_images(
    input_dir: Path,
    output_dir: Path,
    *,
    recursive: bool = False,
    include_jpg: bool = False,
    max_width: int = DEFAULT_MAX_WIDTH,
    max_height: int = DEFAULT_MAX_HEIGHT,
    quality: int = DEFAULT_JPEG_QUALITY,
    pad_digits: int = 3,
    on_progress: Callable[[int, int, ConvertResult | ConvertSkip], None] | None = None,
) -> tuple[list[ConvertResult], list[ConvertSkip]]:
    sources = iter_source_images(input_dir, recursive=recursive, include_jpg=include_jpg)
    out_dir = output_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    planned = _plan_output_numbers(sources)
    skipped: list[ConvertSkip] = []
    by_number: dict[int, Path] = {}

    work_items: list[tuple[Path, int, str]] = []
    for src, n, note in planned:
        if n is None:
            skipped.append(ConvertSkip(src, note))
            continue
        if n in by_number:
            skipped.append(ConvertSkip(src, f"SRT_{n:03d} 중복 (이미 {by_number[n].name})"))
            continue
        by_number[n] = src
        work_items.append((src, n, note))

    results: list[ConvertResult] = []
    total = len(work_items)

    for i, (src, n, note) in enumerate(work_items, start=1):
        dst = out_dir / srt_jpg_name(n, pad=pad_digits)
        try:
            r = convert_one(
                src,
                dst,
                max_width=max_width,
                max_height=max_height,
                quality=quality,
            )
            r.srt_number = n
            r.match_note = note
            results.append(r)
            if on_progress:
                on_progress(i, total, r)
        except OSError as e:
            sk = ConvertSkip(src, str(e))
            skipped.append(sk)
            if on_progress:
                on_progress(i, total, sk)

    return results, skipped


def _output_path_for_source(src: Path, n: int | None, *, pad_digits: int) -> Path:
    """소스와 같은 폴더에 저장할 JPG 경로."""
    if n is not None:
        return src.parent / srt_jpg_name(n, pad=pad_digits)
    return src.parent / f"{src.stem}.jpg"


def convert_selected_files(
    sources: list[Path],
    *,
    include_jpg: bool = True,
    max_width: int = DEFAULT_MAX_WIDTH,
    max_height: int = DEFAULT_MAX_HEIGHT,
    quality: int = DEFAULT_JPEG_QUALITY,
    pad_digits: int = 3,
    on_progress: Callable[[int, int, ConvertResult | ConvertSkip], None] | None = None,
) -> tuple[list[ConvertResult], list[ConvertSkip]]:
    """지정한 이미지를 각 파일이 있는 폴더에 JPG로 저장."""
    allowed = set(PNG_EXTS)
    if include_jpg:
        allowed |= JPG_EXTS

    normalized: list[Path] = []
    skipped: list[ConvertSkip] = []
    seen: set[Path] = set()
    for raw in sources:
        src = raw.expanduser().resolve()
        if src in seen:
            continue
        seen.add(src)
        if not src.is_file():
            skipped.append(ConvertSkip(src, "파일이 없습니다"))
            continue
        if src.suffix in JPG_EXTS and not include_jpg:
            skipped.append(ConvertSkip(src, "JPG 재저장 옵션이 꺼져 있습니다"))
            continue
        if src.suffix not in allowed:
            skipped.append(ConvertSkip(src, f"지원하지 않는 확장자: {src.suffix}"))
            continue
        normalized.append(src)

    planned = _plan_output_numbers(normalized)
    by_number: dict[int, Path] = {}
    work_items: list[tuple[Path, int | None, str]] = []

    for src, n, note in planned:
        if n is None:
            work_items.append((src, None, note))
            continue
        if n in by_number:
            skipped.append(
                ConvertSkip(src, f"SRT_{n:03d} 중복 (이미 {by_number[n].name})")
            )
            continue
        by_number[n] = src
        work_items.append((src, n, note))

    results: list[ConvertResult] = []
    total = len(work_items)

    for i, (src, n, note) in enumerate(work_items, start=1):
        dst = _output_path_for_source(src, n, pad_digits=pad_digits)
        try:
            r = convert_one(
                src,
                dst,
                max_width=max_width,
                max_height=max_height,
                quality=quality,
            )
            if n is not None:
                r.srt_number = n
            r.match_note = note if n is not None else "같은 폴더 · 원본 파일명"
            results.append(r)
            if on_progress:
                on_progress(i, total, r)
        except OSError as e:
            sk = ConvertSkip(src, str(e))
            skipped.append(sk)
            if on_progress:
                on_progress(i, total, sk)

    return results, skipped

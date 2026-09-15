# -*- coding: utf-8 -*-
"""이미지 생성 로그 — log/image.log · log/image_fail.log."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path


def log_dir_for_png(png_dir: Path) -> Path:
    """png 폴더와 형제 log/ 또는 png 상위/log."""
    png_dir = Path(png_dir)
    parent = png_dir.parent
    # …/png → …/log , 그 외 → png_dir/log
    if png_dir.name.lower() == "png":
        d = parent / "log"
    else:
        d = png_dir / "log"
    d.mkdir(parents=True, exist_ok=True)
    return d


def append_image_log(png_dir: Path, message: str) -> None:
    path = log_dir_for_png(png_dir) / "image.log"
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {message.strip()}\n"
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


def append_fail_log(
    png_dir: Path,
    *,
    scene: str,
    error: str,
    kind: str = "fail",
    page_snip: str = "",
    extra: str = "",
) -> Path | None:
    """실패 분석용 ``image_fail.log`` — 시각·씬·종류·에러·페이지 스니펫."""
    path = log_dir_for_png(png_dir) / "image_fail.log"
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    blocks = [
        "=" * 60,
        f"[{ts}] {kind} · {scene}",
        f"error: {(error or '').strip()}",
    ]
    if extra:
        blocks.append(f"extra: {extra.strip()}")
    if page_snip:
        snip = re.sub(r"[ \t]+", " ", (page_snip or "").replace("\r", ""))
        snip = re.sub(r"\n{3,}", "\n\n", snip).strip()
        if len(snip) > 1500:
            snip = snip[:1500] + "…"
        blocks.append("--- page ---")
        blocks.append(snip)
    blocks.append("")
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write("\n".join(blocks))
        return path
    except OSError:
        return None

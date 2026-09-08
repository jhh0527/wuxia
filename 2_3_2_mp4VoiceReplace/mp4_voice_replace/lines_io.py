# -*- coding: utf-8 -*-
"""mp3/lines.json 로드."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class VoiceLine:
    index: int
    file: str
    path: Path
    speaker: str
    text: str


def load_lines_json(mp3_dir: Path | str) -> list[VoiceLine]:
    """``mp3_dir/lines.json`` → 순서대로 VoiceLine (파일 존재 필수)."""
    folder = Path(mp3_dir).expanduser()
    meta = folder / "lines.json"
    if not meta.is_file():
        raise FileNotFoundError(f"lines.json 없음: {meta}")
    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"lines.json 읽기 실패: {e}") from e
    raw_lines = data.get("lines") if isinstance(data, dict) else None
    if not isinstance(raw_lines, list) or not raw_lines:
        raise ValueError("lines.json 에 lines[] 가 없습니다.")

    out: list[VoiceLine] = []
    for i, item in enumerate(raw_lines, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"lines[{i}] 형식이 올바르지 않습니다.")
        idx = int(item.get("index") or i)
        fname = str(item.get("file") or "").strip()
        if not fname:
            fname = f"{idx:02d}.mp3"
        path = folder / fname
        if not path.is_file():
            raise FileNotFoundError(f"음성 파일 없음: {path}")
        out.append(
            VoiceLine(
                index=idx,
                file=fname,
                path=path,
                speaker=str(item.get("speaker") or "").strip(),
                text=str(item.get("text") or "").strip(),
            )
        )
    return out

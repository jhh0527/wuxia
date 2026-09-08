# -*- coding: utf-8 -*-
"""SRT 큐 파싱."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_TS = re.compile(r"^(\d{2}):(\d{2}):(\d{2})[,.](\d{3})$")


@dataclass(frozen=True)
class SrtCue:
    index: int
    start_ms: int
    end_ms: int
    text: str


def parse_srt_timestamp_ms(ts: str) -> int:
    raw = ts.strip().replace(".", ",")
    m = _TS.match(raw)
    if not m:
        raise ValueError(f"SRT 타임스탬프 형식이 아닙니다: {ts!r}")
    h, mi, s, z = (int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)))
    return ((h * 60 + mi) * 60 + s) * 1000 + z


def parse_srt_cues(path: Path | str) -> list[SrtCue]:
    p = Path(path)
    raw = p.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n").strip()
    cues: list[SrtCue] = []
    if not raw:
        return cues
    for block in raw.split("\n\n"):
        lines = [ln for ln in block.strip().split("\n")]
        if len(lines) < 2 or "-->" not in lines[1]:
            continue
        left, _, right = lines[1].partition("-->")
        try:
            st = parse_srt_timestamp_ms(left)
            end_part = right.strip().split()[0] if right.strip() else left
            en = parse_srt_timestamp_ms(end_part)
        except ValueError:
            continue
        head = lines[0].strip()
        idx = int(head) if head.isdigit() else len(cues) + 1
        text = "\n".join(lines[2:]).strip() if len(lines) > 2 else ""
        cues.append(SrtCue(index=idx, start_ms=st, end_ms=max(en, st), text=text))
    return cues

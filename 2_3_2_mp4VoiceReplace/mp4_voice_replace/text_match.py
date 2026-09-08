# -*- coding: utf-8 -*-
"""lines.json 대사 ↔ SRT 큐 텍스트 매칭 → 배치 시작(ms)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from mp4_voice_replace.ffmpeg_util import probe_duration_sec
from mp4_voice_replace.lines_io import VoiceLine
from mp4_voice_replace.srt_parse import SrtCue

_PUNCT = re.compile(r"[\s\u3000.,!?;:，。！？、…·~～\-—_/\\\"'“”‘’\[\]()（）【】<>《》]+")


def normalize_text(s: str) -> str:
    t = _PUNCT.sub("", (s or "").strip().casefold())
    return t


@dataclass(frozen=True)
class Placement:
    line: VoiceLine
    start_ms: int
    cue_from: int
    cue_to: int
    how: str


def _span_score(line_n: str, joined: str, i: int, j: int) -> float | None:
    if not line_n or not joined:
        return None
    span_len = j - i
    if line_n == joined:
        return 100_000.0 + len(joined) - span_len * 0.01
    if line_n in joined:
        # 가장 짧은 구간에 줄이 들어가면 가점 (합쳐진 큐 안 부분 대사)
        slack = len(joined) - len(line_n)
        return 50_000.0 + len(line_n) - slack - span_len * 10.0
    if joined in line_n:
        # 쪼개진 큐들을 모을수록 가점
        cover = len(joined) / max(1, len(line_n))
        if cover < 0.25 and len(joined) < 4:
            return None
        return 10_000.0 + len(joined) * 10.0 - span_len * 0.1
    return None


def find_best_cue_span(line_n: str, cues: list[SrtCue]) -> tuple[int, int, float] | None:
    best: tuple[int, int, float] | None = None
    n = len(cues)
    for i in range(n):
        joined = ""
        for j in range(i, n):
            joined += normalize_text(cues[j].text)
            sc = _span_score(line_n, joined, i, j)
            if sc is None:
                continue
            if best is None or sc > best[2]:
                best = (i, j, sc)
    return best


def match_placements(
    cues: list[SrtCue],
    lines: list[VoiceLine],
) -> list[Placement]:
    """대사 텍스트로 줄→큐 구간 매칭 후, 동일 큐에 여러 줄이면 mp3 길이만큼 이어 배치."""
    if not cues:
        raise ValueError("SRT 큐가 비어 있습니다.")
    if not lines:
        raise ValueError("lines.json 줄이 비어 있습니다.")

    raw: list[tuple[VoiceLine, int, int, str]] = []
    unmatched: list[str] = []

    for line in lines:
        ln = normalize_text(line.text)
        if not ln:
            unmatched.append(f"[{line.index}] (빈 대사)")
            continue
        hit = find_best_cue_span(ln, cues)
        if hit is None:
            unmatched.append(f"[{line.index}] {line.speaker}: {line.text[:40]}")
            continue
        i, j, _sc = hit
        how = "exact/span"
        cl = "".join(normalize_text(cues[k].text) for k in range(i, j + 1))
        if ln == cl:
            how = "exact"
        elif ln in cl:
            how = "line⊂cue"
        elif cl in ln:
            how = "cue⊂line"
        raw.append((line, i, j, how))

    if unmatched:
        raise ValueError(
            "SRT 대사와 매칭되지 않은 줄이 있습니다:\n  - "
            + "\n  - ".join(unmatched)
        )

    # 시작 ms 후보 = 구간 첫 큐. 같은 cue_from 이면 mp3 길이로 순차.
    raw.sort(key=lambda x: (x[1], x[0].index))
    out: list[Placement] = []
    idx = 0
    while idx < len(raw):
        line0, i0, j0, how0 = raw[idx]
        group = [raw[idx]]
        idx += 1
        while idx < len(raw) and raw[idx][1] == i0:
            group.append(raw[idx])
            idx += 1

        base = int(cues[i0].start_ms)
        cursor = base
        for line, i, j, how in group:
            start = cursor if len(group) > 1 else base
            # 그룹이 1개여도 base 사용
            if len(group) == 1:
                start = base
            out.append(
                Placement(
                    line=line,
                    start_ms=max(0, int(start)),
                    cue_from=i,
                    cue_to=j,
                    how=how,
                )
            )
            if len(group) > 1:
                dur = probe_duration_sec(line.path)
                add_ms = int(round((dur or 0.5) * 1000))
                cursor = start + max(50, add_ms)

    out.sort(key=lambda p: (p.start_ms, p.line.index))
    return out


def format_match_summary(placements: list[Placement], *, n_cues: int) -> str:
    n = len(placements)
    bits = [f"텍스트 매칭 OK — {n}줄 ↔ SRT {n_cues}큐"]
    for p in placements[:8]:
        bits.append(
            f"  · [{p.line.index}] {p.line.file} @ {p.start_ms}ms "
            f"(큐{p.cue_from + 1}"
            + (f"-{p.cue_to + 1}" if p.cue_to != p.cue_from else "")
            + f", {p.how})"
        )
    if n > 8:
        bits.append(f"  · … 외 {n - 8}줄")
    return "\n".join(bits)

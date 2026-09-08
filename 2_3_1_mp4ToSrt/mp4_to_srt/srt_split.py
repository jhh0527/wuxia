# -*- coding: utf-8 -*-
"""SRT 큐를 20~25자(공백 포함, 상한 25)로 분할."""

from __future__ import annotations

import re
from dataclasses import dataclass

TARGET_MIN = 20
TARGET_MAX = 25

_BREAK_CHARS = set(" \t,.!?;:，。！？、…·~-—/")


@dataclass(frozen=True)
class TimedWord:
    text: str
    start: float
    end: float


@dataclass(frozen=True)
class SrtCue:
    index: int
    start: float
    end: float
    text: str


def char_len(s: str) -> int:
    return len(s or "")


def format_srt_ts(sec: float) -> str:
    sec = max(0.0, float(sec))
    ms = int(round(sec * 1000))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, z = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{z:03d}"


def cues_to_srt(cues: list[SrtCue]) -> str:
    lines: list[str] = []
    for c in cues:
        lines.append(str(c.index))
        lines.append(f"{format_srt_ts(c.start)} --> {format_srt_ts(c.end)}")
        lines.append(c.text.strip())
        lines.append("")
    return "\n".join(lines).rstrip() + ("\n" if cues else "")


def _soft_break_score(ch: str) -> int:
    if ch in ".!?。！？":
        return 3
    if ch in ",，、;:…":
        return 2
    if ch in _BREAK_CHARS:
        return 1
    return 0


def _join_cue_text(a: str, b: str) -> str:
    a = (a or "").rstrip()
    b = (b or "").lstrip()
    if not a:
        return b
    if not b:
        return a
    return f"{a} {b}"


def split_words_to_cues(
    words: list[TimedWord],
    *,
    min_chars: int = TARGET_MIN,
    max_chars: int = TARGET_MAX,
) -> list[SrtCue]:
    if not words:
        return []
    min_chars = max(1, int(min_chars))
    max_chars = max(min_chars, int(max_chars))

    cues: list[SrtCue] = []
    buf: list[TimedWord] = []

    def buf_text() -> str:
        return "".join(w.text for w in buf)

    def flush(force: bool = False) -> None:
        nonlocal buf
        if not buf:
            return
        text = buf_text().strip()
        if not text:
            buf = []
            return
        n = char_len(text)
        if not force and n < min_chars and cues:
            prev = cues[-1]
            merged = _join_cue_text(prev.text, text)
            if char_len(merged) <= max_chars:
                cues[-1] = SrtCue(
                    index=prev.index,
                    start=prev.start,
                    end=buf[-1].end,
                    text=merged,
                )
                buf = []
                return
        cues.append(
            SrtCue(
                index=len(cues) + 1,
                start=buf[0].start,
                end=buf[-1].end,
                text=text,
            )
        )
        buf = []

    for w in words:
        piece = w.text
        if not piece:
            continue
        trial = buf_text() + piece
        if char_len(trial) <= max_chars:
            buf.append(w)
            t = buf_text()
            if char_len(t) >= min_chars and _soft_break_score(t[-1]) >= 1:
                flush(force=True)
            continue

        if buf:
            flush(force=True)
        if char_len(piece) <= max_chars:
            buf.append(w)
            continue
        dur = max(0.01, w.end - w.start)
        for i in range(0, len(piece), max_chars):
            chunk = piece[i : i + max_chars]
            frac0 = i / len(piece)
            frac1 = min(1.0, (i + len(chunk)) / len(piece))
            cues.append(
                SrtCue(
                    index=len(cues) + 1,
                    start=w.start + dur * frac0,
                    end=w.start + dur * frac1,
                    text=chunk.strip() or chunk,
                )
            )

    flush(force=True)

    merged: list[SrtCue] = []
    for c in cues:
        if not merged:
            merged.append(c)
            continue
        prev = merged[-1]
        joined = _join_cue_text(prev.text, c.text)
        if char_len(c.text) < min_chars and char_len(joined) <= max_chars:
            merged[-1] = SrtCue(
                index=prev.index,
                start=prev.start,
                end=c.end,
                text=joined,
            )
            continue
        if char_len(prev.text) < min_chars and char_len(joined) <= max_chars:
            merged[-1] = SrtCue(
                index=prev.index,
                start=prev.start,
                end=c.end,
                text=joined,
            )
            continue
        merged.append(c)

    if len(merged) >= 2:
        prev, last = merged[-2], merged[-1]
        joined = _join_cue_text(prev.text, last.text)
        if char_len(last.text) < min_chars and char_len(joined) <= max_chars:
            merged[-2] = SrtCue(
                index=prev.index,
                start=prev.start,
                end=last.end,
                text=joined,
            )
            merged.pop()

    out: list[SrtCue] = []
    for i, c in enumerate(merged, start=1):
        out.append(SrtCue(index=i, start=c.start, end=c.end, text=c.text.strip()))
    return out


def split_segment_text_timed(
    text: str,
    start: float,
    end: float,
    *,
    min_chars: int = TARGET_MIN,
    max_chars: int = TARGET_MAX,
) -> list[SrtCue]:
    text = re.sub(r"\s+", " ", (text or "").strip())
    if not text:
        return []
    if char_len(text) <= max_chars:
        return [SrtCue(index=1, start=start, end=end, text=text)]

    chunks: list[str] = []
    buf = ""
    for ch in text:
        buf += ch
        if char_len(buf) >= min_chars and (
            _soft_break_score(ch) >= 1 or char_len(buf) >= max_chars
        ):
            if char_len(buf) > max_chars:
                keep = buf[:-1]
                rest = buf[-1]
                while keep and char_len(keep) > max_chars:
                    chunks.append(keep[:max_chars])
                    keep = keep[max_chars:]
                if keep:
                    chunks.append(keep)
                buf = rest
            else:
                chunks.append(buf.strip())
                buf = ""
    if buf.strip():
        if chunks and char_len(chunks[-1] + buf.strip()) <= max_chars:
            chunks[-1] = (chunks[-1] + buf).strip()
        else:
            while char_len(buf) > max_chars:
                chunks.append(buf[:max_chars])
                buf = buf[max_chars:]
            if buf.strip():
                chunks.append(buf.strip())

    total = sum(char_len(c) for c in chunks) or 1
    dur = max(0.01, end - start)
    cues: list[SrtCue] = []
    acc = 0
    for i, ch in enumerate(chunks):
        n = char_len(ch)
        t0 = start + dur * (acc / total)
        acc += n
        t1 = start + dur * (acc / total) if i < len(chunks) - 1 else end
        cues.append(SrtCue(index=i + 1, start=t0, end=t1, text=ch))
    return cues

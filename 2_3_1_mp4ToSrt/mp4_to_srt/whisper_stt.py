# -*- coding: utf-8 -*-
"""faster-whisper 로 MP4/영상 음성 인식 → SRT."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from mp4_to_srt.srt_split import TimedWord, cues_to_srt, split_segment_text_timed, split_words_to_cues

ProgressCb = Callable[[str, float], None]

_model_cache: dict[str, object] = {}


def has_faster_whisper() -> bool:
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        return False
    return True


def load_model(model_size: str = "base", *, device: str = "cpu", compute_type: str = "int8"):
    key = f"{model_size}:{device}:{compute_type}"
    if key in _model_cache:
        return _model_cache[key]
    from faster_whisper import WhisperModel

    model = WhisperModel(model_size, device=device, compute_type=compute_type)
    _model_cache[key] = model
    return model


def transcribe_to_words(
    media_path: Path | str,
    *,
    model_size: str = "base",
    language: str = "ko",
    on_progress: ProgressCb | None = None,
) -> list[TimedWord]:
    if not has_faster_whisper():
        raise RuntimeError(
            "faster-whisper 가 필요합니다.\n"
            "pip install faster-whisper"
        )
    path = Path(media_path)
    if not path.is_file():
        raise FileNotFoundError(str(path))

    if on_progress:
        on_progress(f"모델 로드 ({model_size})…", 2.0)
    model = load_model(model_size)

    if on_progress:
        on_progress(f"인식 중… {path.name}", 5.0)
    segments, info = model.transcribe(
        str(path),
        language=language or None,
        word_timestamps=True,
        vad_filter=True,
    )
    duration = float(getattr(info, "duration", 0) or 0)

    words: list[TimedWord] = []
    fallback_cues_words: list[TimedWord] = []
    seg_i = 0

    for seg in segments:
        seg_i += 1
        seg_words = getattr(seg, "words", None) or []
        if seg_words:
            for w in seg_words:
                t = (getattr(w, "word", None) or "").replace("\n", " ")
                if not t:
                    continue
                st = float(getattr(w, "start", None) or seg.start or 0.0)
                en = float(getattr(w, "end", None) or seg.end or st)
                words.append(TimedWord(text=t, start=st, end=max(en, st + 0.01)))
        else:
            text = (seg.text or "").strip()
            if not text:
                continue
            for cue in split_segment_text_timed(text, float(seg.start), float(seg.end)):
                fallback_cues_words.append(
                    TimedWord(text=cue.text, start=cue.start, end=cue.end)
                )
        if on_progress:
            if duration > 0:
                pct = 5.0 + 85.0 * min(1.0, float(seg.end or 0) / duration)
                on_progress(f"인식 중… {path.name} ({pct:.0f}%)", pct)
            else:
                pct = min(90.0, 5.0 + seg_i * 2.0)
                on_progress(f"인식 중… {path.name} · 구간 {seg_i}", pct)

    if words:
        return words
    return fallback_cues_words


def transcribe_to_srt_text(
    media_path: Path | str,
    *,
    model_size: str = "base",
    language: str = "ko",
    min_chars: int = 20,
    max_chars: int = 25,
    on_progress: ProgressCb | None = None,
) -> str:
    words = transcribe_to_words(
        media_path,
        model_size=model_size,
        language=language,
        on_progress=on_progress,
    )
    if on_progress:
        on_progress("20~25자 자막 분할…", 92.0)
    cues = split_words_to_cues(words, min_chars=min_chars, max_chars=max_chars)
    if on_progress:
        on_progress(f"SRT 큐 {len(cues)}개", 98.0)
    return cues_to_srt(cues)

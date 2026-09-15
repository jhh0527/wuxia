# -*- coding: utf-8 -*-
"""2_2_scriptToVoice: ElevenLabs TTS HTTP 호출 + MP3 병합(ffmpeg/바이너리)."""

from __future__ import annotations

import http.client
import json
import math
import re
import shutil
import ssl
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

DEFAULT_HOST = "api.elevenlabs.io"
# ElevenLabs MP3 최소 크기(빈·잘린 응답 거부). ID3 헤더만 있는 경우도 걸러냄.
_MIN_MP3_BYTES = 256
# ElevenLabs 세그먼트와 동일하게 맞춤 (경계 ID3·DTS 꼬임 방지용 재인코딩)
_FFMPEG_MP3_ENCODE_ARGS = ["-c:a", "libmp3lame", "-b:a", "128k", "-ar", "44100", "-ac", "1"]

# 줄 끝 [breathes] (파트 경계 ``[short pause]`` 조합은 아래 1.0s 유지)
LINE_BREATH_BREAK = "0.5s"
# v3: 후행 [short pause] 자동 부여는 말미 잡음 유발 → 기본 비활성
V3_TRAILING_PAUSE = "[short pause]"
# (비활성) 이 글자 수 미만일 때만 후행 pause 하던 기준
V3_TRAILING_PAUSE_MAX_CHARS = 50
# 트림 후 남겨 둘 말미 무음(초)
V3_KEEP_TRAIL_SILENCE_SEC = 0.28
# 합성 클립 말미 fade-out (초) — 끝 클릭·글리치 완화
TRAILING_FADE_OUT_SEC = 0.07

_BRACKET_TAG_RE = re.compile(r"\[[^\]]*\]", re.IGNORECASE)
_BREAK_TAG_RE = re.compile(r"<break\s+[^>]*?/?>", re.IGNORECASE)
_V3_PAUSE_AT_END_RE = re.compile(
    r"\[(?:short\s+pause|long\s+pause|pause)\]\s*$",
    re.IGNORECASE,
)
# 마침표 뒤 다음 문장 앞에 쉼 (이미 pause 태그면 생략)
_SENTENCE_BOUNDARY_PAUSE_RE = re.compile(
    r"([.!?。！？])(?!\s*\[(?:short\s+pause|long\s+pause|pause|breathes)\])\s+"
    r"(?=[\"'“‘\[\(]?\S)",
    re.IGNORECASE,
)
# v3 에서 유지할 오디오 태그 (그 외 대괄호는 제거)
_V3_KEEP_TAG_RE = re.compile(
    r"^\[(?:"
    r"short\s+pause|long\s+pause|pause|"
    r"happy|happily|excited|sad|sorrowful|angry|worried|nervous|"
    r"frustrated|tired|calm|curious|playfully|resigned\s+tone|"
    r"flatly|sarcastically|mischievously|crying|"
    r"whispers?|quietly|shouts?|low\s+voice|"
    r"laughs?(?:\s+harder)?|light\s+chuckle|chuckles?|"
    r"sigh(?:\s+of\s+relief)?|sighs?|exhales?|inhales?|"
    r"gasps?|hesitates|stammers|"
    r"rushed|slowly|soft|intense|emphasis|drawn\s+out|"
    r"clears\s+throat|breathes|deep\s+breath|"
    # 대본에서 자주 쓰는 감정 (……! 반응 등)
    r"surprised|stern|cheerful|cold|bitter|anxious|shy|"
    r"serious|concerned|hesitant|trembling|timid|dismissive|"
    r"approving|forced\s+calm"
    r")\]$",
    re.IGNORECASE,
)
# 숫자·라틴·히라가나/가타카나·CJK·한글 — 구두점·따옴표·말줄임만 있으면 비낭독
_SPEAKABLE_RE = re.compile(
    r"[0-9A-Za-z\u00C0-\u024F\u3040-\u30FF\u3400-\u9FFF\uAC00-\uD7AF]"
)


def is_eleven_v3(model_id: str) -> bool:
    return "v3" in (model_id or "").lower()


def strip_tts_tags(text: str) -> str:
    """길이 추정·SRT용: 대괄호 태그를 제거한 낭독 텍스트."""
    return _BRACKET_TAG_RE.sub("", text)


# 대본 ―― / —— — ElevenLabs가 기호로 읽거나 억양이 튀는 경우가 많음
_DASH_RUN_RE = re.compile(r"[—–―－\-]{2,}")


def normalize_dash_punctuation(text: str, *, for_v3: bool) -> str:
    """``――`` ``——`` 등 대시를 TTS 친화 구두점·pause 로 치환."""
    s = (text or "").strip()
    if not _DASH_RUN_RE.search(s):
        return s
    pause = "[short pause] " if for_v3 else '<break time="0.4s" /> '
    # 줄머리 박자: ``――셋.`` → ``[short pause] 셋.``
    if _DASH_RUN_RE.match(s):
        s = _DASH_RUN_RE.sub(pause, s, count=1)
    # 말끊김·잔여: ``하지만――`` → ``하지만...``
    s = _DASH_RUN_RE.sub("...", s)
    return s


def prepare_tts_for_api(
    text: str, *, model_id: str = "", add_trailing_pause: bool = False
) -> str:
    """ElevenLabs API용 텍스트.

    - 비-v3: ``[breathes]`` 등 → SSML ``<break>``
    - eleven_v3: SSML 미지원 → ``[short pause]`` 등 오디오 태그 유지
    - 후행 ``[short pause]`` 자동 부여는 기본 끔 (말미 잡음).
      ``add_trailing_pause=True`` 일 때만 짧은 문장에 부여
    """
    s = text.strip()
    s = re.sub(
        r"^\s*(?:<break\s+time=\"[^\"]+\"\s*/>\s*)+",
        "",
        s,
        flags=re.IGNORECASE,
    )
    v3 = is_eleven_v3(model_id)
    s = normalize_dash_punctuation(s, for_v3=v3)

    if v3:
        # 조합 태그를 짧은 쉼으로 정리
        s = re.sub(
            r"\[short pause\]\s*\[breathes\]\s*\[continues\]",
            "[short pause]",
            s,
            flags=re.IGNORECASE,
        )
        s = re.sub(
            r"\[short pause\]\s*\[breathes\]",
            "[short pause]",
            s,
            flags=re.IGNORECASE,
        )
        s = re.sub(r"\[breathes\]", "[short pause]", s, flags=re.IGNORECASE)
        s = re.sub(r"\[continues\]", "", s, flags=re.IGNORECASE)

        def _v3_tag(m: re.Match[str]) -> str:
            raw = m.group(0)
            return raw if _V3_KEEP_TAG_RE.match(raw.strip()) else ""

        s = _BRACKET_TAG_RE.sub(_v3_tag, s)
        s = " ".join(s.split()).strip()
        # 마침표·문장부호 뒤 다음 문장 연결 시 쉼
        s = _SENTENCE_BOUNDARY_PAUSE_RE.sub(r"\1 [short pause] ", s)
        s = " ".join(s.split()).strip()
        # 짧은 문장 후행 pause — 기본 비활성 (말미 잡음)
        if add_trailing_pause:
            speakable = _BRACKET_TAG_RE.sub("", s)
            speakable = " ".join(speakable.split())
            if (
                s
                and len(speakable) < V3_TRAILING_PAUSE_MAX_CHARS
                and not _V3_PAUSE_AT_END_RE.search(s)
            ):
                s = f"{s} {V3_TRAILING_PAUSE}"
        return s

    # 비-v3: SSML break
    s = re.sub(
        r"\[short pause\]\s*\[breathes\]\s*\[continues\]",
        '<break time="1.0s" />',
        s,
        flags=re.IGNORECASE,
    )
    s = re.sub(
        r"\[short pause\]\s*\[breathes\]",
        '<break time="1.0s" />',
        s,
        flags=re.IGNORECASE,
    )
    s = re.sub(r"\[short pause\]", '<break time="0.4s" />', s, flags=re.IGNORECASE)
    s = re.sub(
        r"\[breathes\]",
        f'<break time="{LINE_BREATH_BREAK}" />',
        s,
        flags=re.IGNORECASE,
    )
    s = re.sub(r"\[continues\]", "", s, flags=re.IGNORECASE)
    s = _BRACKET_TAG_RE.sub("", s)
    # 마침표 뒤 다음 문장 연결 시 break
    s = re.sub(
        r'([.!?。！？])(?!\s*<break)\s+(?=[\"\'“‘\[\(]?\S)',
        r'\1 <break time="0.45s" /> ',
        s,
    )
    return s.strip()


def api_text_has_speech(prepared: str) -> bool:
    """API용 준비 문자열에 실제 낭독할 글자가 있는지 (break·구두점만이면 False)."""
    s = _BREAK_TAG_RE.sub(" ", prepared or "")
    s = _BRACKET_TAG_RE.sub(" ", s)
    return bool(_SPEAKABLE_RE.search(s))


def silence_sec_from_prepared(prepared: str, *, default_sec: float = 0.5) -> float:
    """``<break time=\"Ns\"/>`` 합산 초. 없으면 default."""
    times = re.findall(
        r'<break\s+time="([0-9]*\.?[0-9]+)s"', prepared or "", flags=re.IGNORECASE
    )
    if times:
        return max(0.05, min(3.0, sum(float(t) for t in times)))
    return max(0.05, min(3.0, float(default_sec)))


def _looks_like_mp3(data: bytes) -> bool:
    if len(data) < _MIN_MP3_BYTES:
        return False
    if data[:3] == b"ID3":
        return True
    # MPEG frame sync (mp3)
    return data[0] == 0xFF and (data[1] & 0xE0) == 0xE0


def synthesize_mp3(
    api_key: str,
    voice_id: str,
    text: str,
    *,
    model_id: str = "eleven_multilingual_v2",
    add_trailing_pause: bool = False,
    timeout: int = 120,
    retries: int = 3,
) -> bytes:
    """ElevenLabs TTS. 요청마다 인터페이스 설정을 초기화합니다 (문맥·voice_settings 미전달)."""
    mid = (model_id or "eleven_multilingual_v2").strip()
    plain = prepare_tts_for_api(
        text, model_id=mid, add_trailing_pause=add_trailing_pause
    )
    if not plain or not api_text_has_speech(plain):
        preview = (text or "").replace("\n", " ").strip()
        if len(preview) > 60:
            preview = preview[:60] + "…"
        raise ValueError(
            "낭독할 글자가 없습니다 (감정 태그·구두점·말줄임만). "
            "이 줄은 무음으로 처리할 수 있습니다.\n"
            f"원문: {preview or '(빈 문자열)'}"
        )
    vid = quote(voice_id, safe="-._~")
    # output_format: 품질·길이 안정화
    path = f"/v1/text-to-speech/{vid}?output_format=mp3_44100_128"
    # previous/next_text · voice_settings 등 세션 설정을 넘기지 않음 (매 호출 초기화)
    body: dict = {
        "text": plain,
        "model_id": mid,
    }

    # ensure_ascii=True: 일부 환경에서 HTTP 스택이 본문을 ASCII로 다루는 문제 회피 (API는 \\u 이스케이프 허용)
    payload = json.dumps(
        body,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")

    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "audio/mpeg",
        "Content-Length": str(len(payload)),
        "Connection": "close",
    }

    last_err: Exception | None = None
    attempts = max(1, int(retries))
    for attempt in range(1, attempts + 1):
        ctx = ssl.create_default_context()
        conn = http.client.HTTPSConnection(DEFAULT_HOST, timeout=timeout, context=ctx)
        try:
            conn.request("POST", path, body=payload, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            if resp.status == 429 or resp.status >= 500:
                err = data.decode("utf-8", errors="replace").strip()
                last_err = RuntimeError(
                    f"ElevenLabs API 오류 {resp.status}: {err or '(empty)'}"
                )
            elif resp.status >= 400:
                err = data.decode("utf-8", errors="replace")
                raise RuntimeError(f"ElevenLabs API 오류 {resp.status}: {err}")
            elif not _looks_like_mp3(data):
                preview = data[:120].decode("utf-8", errors="replace").strip()
                last_err = RuntimeError(
                    f"ElevenLabs 빈·비정상 MP3 응답 ({len(data)} bytes)"
                    + (f": {preview}" if preview else "")
                )
            else:
                return data
        except (TimeoutError, OSError, http.client.HTTPException) as e:
            last_err = e
        finally:
            try:
                conn.close()
            except Exception:
                pass
        if attempt < attempts:
            time.sleep(min(8.0, 1.5 * attempt))
    raise RuntimeError(
        f"음성 합성 실패 ({attempts}회 시도): {last_err}"
    ) from last_err


def trim_trailing_silence_mp3(
    mp3_path: Path,
    *,
    keep_silence_sec: float = V3_KEEP_TRAIL_SILENCE_SEC,
    threshold_db: float = -45.0,
) -> None:
    """말미 무음·끝 잡음 스파이크 앞의 긴 침묵을 자릅니다.

    ``areverse`` 방식은 파일 끝 클릭/잡음이 있으면 긴 말미 무음을 못 자릅니다.
    ``stop_periods=-1`` 로 끝에서부터 침묵을 제거합니다.
    """
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        return
    src = Path(mp3_path).resolve()
    if not src.is_file() or src.stat().st_size < _MIN_MP3_BYTES:
        return
    keep = max(0.05, min(0.5, float(keep_silence_sec)))
    thr = float(threshold_db)
    if thr > -20.0:
        thr = -20.0
    if thr < -70.0:
        thr = -70.0
    tmp = src.with_suffix(".trim_tmp.mp3")
    # 끝 잡음 스파이크가 있어도 그 앞의 긴 침묵을 제거
    af = (
        f"silenceremove=stop_periods=-1:stop_duration=0.2:"
        f"stop_threshold={thr:.1f}dB:stop_silence={keep:.3f}"
    )
    kw: dict = dict(
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    if sys.platform == "win32":
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    r = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(src),
            "-af",
            af,
            *_FFMPEG_MP3_ENCODE_ARGS,
            str(tmp),
        ],
        **kw,
    )
    if r.returncode != 0 or not tmp.is_file() or tmp.stat().st_size < _MIN_MP3_BYTES:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return
    # 트림 결과가 원본보다 터무니없이 짧으면(과도 절단) 원본 유지
    if tmp.stat().st_size < max(_MIN_MP3_BYTES * 2, int(src.stat().st_size * 0.15)):
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return
    tmp.replace(src)


def fade_out_trailing_mp3(
    mp3_path: Path,
    *,
    duration_sec: float = TRAILING_FADE_OUT_SEC,
) -> bool:
    """말미 ``duration_sec`` 동안 볼륨을 0으로 fade-out.

    ElevenLabs 말미 클릭·글리치를 부드럽게 가립니다.
    반환: 적용 성공 여부.
    """
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        return False
    src = Path(mp3_path).resolve()
    if not src.is_file() or src.stat().st_size < _MIN_MP3_BYTES:
        return False
    fade = max(0.04, min(0.15, float(duration_sec)))
    tmp = src.with_suffix(".fade_tmp.mp3")
    # 끝에서 fade: reverse → fade-in → reverse
    af = f"areverse,afade=t=in:st=0:d={fade:.3f},areverse"
    kw: dict = dict(
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    if sys.platform == "win32":
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    r = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(src),
            "-af",
            af,
            *_FFMPEG_MP3_ENCODE_ARGS,
            str(tmp),
        ],
        **kw,
    )
    if r.returncode != 0 or not tmp.is_file() or tmp.stat().st_size < _MIN_MP3_BYTES:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    if tmp.stat().st_size < max(_MIN_MP3_BYTES * 2, int(src.stat().st_size * 0.15)):
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    tmp.replace(src)
    return True


def _pcm_rms_db(chunk: list[int] | tuple[int, ...]) -> float:
    if not chunk:
        return -120.0
    mean_sq = sum(x * x for x in chunk) / len(chunk)
    if mean_sq <= 1e-12:
        return -120.0
    return 20.0 * math.log10((mean_sq**0.5) / 32768.0)


def _pcm_zcr_ratio(chunk: list[int] | tuple[int, ...]) -> float:
    if len(chunk) < 2:
        return 0.0
    zc = sum(1 for a, b in zip(chunk, chunk[1:]) if (a >= 0) != (b >= 0))
    return zc / (len(chunk) - 1)


def _window_kind(rms_db: float, zcr: float) -> str:
    """quiet | speech | spike — ZCR 로 음성/스파이크 구분."""
    if rms_db < -50.0:
        return "quiet"
    # 유성 음성: 에너지 있어도 ZCR 낮음. 클릭/버스트: ZCR 높음.
    if zcr >= 0.12 and rms_db > -45.0:
        return "spike"
    if rms_db >= -40.0 and zcr < 0.12:
        return "speech"
    if rms_db >= -35.0:
        return "speech"
    return "quiet"


def mute_trailing_spike_mp3(mp3_path: Path) -> bool:
    """파일 **맨 끝**의 글리치 꼬리만 무음 처리.

    중반 문장 쉼·다음 절 앞에서는 동작하지 않습니다
    (줄바꿈/호흡 직전에 본편 음성이 잘리는 오인 방지).
    """
    import struct
    import wave

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        return False
    src = Path(mp3_path).resolve()
    if not src.is_file() or src.stat().st_size < _MIN_MP3_BYTES:
        return False

    kw: dict = dict(
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    if sys.platform == "win32":
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]

    wav_in = src.with_suffix(".spike_in.wav")
    wav_out = src.with_suffix(".spike_out.wav")
    mp3_tmp = src.with_suffix(".spike_tmp.mp3")
    try:
        r = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(src),
                "-ac",
                "1",
                "-ar",
                "44100",
                str(wav_in),
            ],
            **kw,
        )
        if r.returncode != 0 or not wav_in.is_file():
            return False

        with wave.open(str(wav_in), "rb") as w:
            rate = w.getframerate()
            nch, sw = w.getnchannels(), w.getsampwidth()
            if nch != 1 or sw != 2:
                return False
            raw = w.readframes(w.getnframes())
        samples = list(struct.unpack("<" + "h" * (len(raw) // 2), raw))
        n = len(samples)
        if n < rate:
            return False

        win = max(64, int(rate * 0.05))
        # 끝 1.2초만 검사 — 그 앞의 quiet 는 문장 경계로 본다
        tail_from = max(0, n - int(rate * 1.2))
        kinds: list[str] = []
        peaks: list[int] = []
        offsets: list[int] = []
        for i in range(0, n, win):
            if i + win <= tail_from:
                continue
            chunk = samples[i : i + win]
            if len(chunk) < win // 2:
                break
            rms_db = _pcm_rms_db(chunk)
            zcr = _pcm_zcr_ratio(chunk)
            kinds.append(_window_kind(rms_db, zcr))
            peaks.append(max(abs(x) for x in chunk))
            offsets.append(i)

        if len(kinds) < 4:
            return False

        candidates: list[int] = []
        i = 0
        while i < len(kinds):
            if kinds[i] != "quiet":
                i += 1
                continue
            j = i
            while j < len(kinds) and kinds[j] == "quiet":
                j += 1
            gap_at = offsets[i]
            gap_samples = (j - i) * win
            if j >= len(kinds):
                # quiet 가 파일 끝까지 — 트림이 담당, mute 불필요
                break
            remain_sec = (n - offsets[j]) / float(rate)
            if not (
                j - i >= 2
                and gap_samples >= int(rate * 0.06)
                and remain_sec <= 1.0
            ):
                i = max(j, i + 1)
                continue

            rest = kinds[j:]
            head = rest[:8]
            first_is_spike = bool(head) and head[0] == "spike"
            spike_soon = "spike" in head[:3]
            spike_rest = sum(1 for t in rest if t == "spike")
            speech_rest = sum(1 for t in rest if t == "speech")
            max_consec = 0
            consec = 0
            for t in rest:
                if t == "speech":
                    consec += 1
                    max_consec = max(max_consec, consec)
                else:
                    consec = 0
            # 갭 뒤가 제대로 된 대사이면 유지
            if head and head[0] == "speech" and max_consec >= 3:
                i = max(j, i + 1)
                continue
            glitch = (
                spike_rest >= 2
                and (first_is_spike or spike_soon)
                and speech_rest <= 4
            )
            if glitch:
                candidates.append(gap_at)
            i = max(j, i + 1)

        if not candidates:
            return False

        mute_at = min(n, candidates[-1])
        fade = max(32, int(rate * 0.012))
        for idx in range(mute_at, n):
            if idx < mute_at + fade:
                samples[idx] = int(samples[idx] * (1.0 - (idx - mute_at) / fade))
            else:
                samples[idx] = 0

        with wave.open(str(wav_out), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(struct.pack("<" + "h" * len(samples), *samples))

        r2 = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(wav_out),
                *_FFMPEG_MP3_ENCODE_ARGS,
                str(mp3_tmp),
            ],
            **kw,
        )
        if (
            r2.returncode != 0
            or not mp3_tmp.is_file()
            or mp3_tmp.stat().st_size < _MIN_MP3_BYTES
        ):
            return False
        mp3_tmp.replace(src)
        return True
    finally:
        for p in (wav_in, wav_out, mp3_tmp):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass


def write_silence_mp3(mp3_path: Path, silence_sec: float) -> None:
    """무음만 있는 MP3를 만듭니다 (태그만 있는 줄·선행 쉼 전용)."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError(
            "ffmpeg 가 필요합니다. 무음 MP3 생성을 위해 PATH에 ffmpeg 를 넣으세요."
        )
    silence_sec = max(0.05, min(3.0, float(silence_sec)))
    out = Path(mp3_path).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    kw: dict = dict(
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    if sys.platform == "win32":
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    r = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-t",
            f"{silence_sec:.3f}",
            "-i",
            "anullsrc=r=44100:cl=mono",
            *_FFMPEG_MP3_ENCODE_ARGS,
            str(out),
        ],
        **kw,
    )
    if r.returncode != 0 or not out.is_file() or out.stat().st_size < _MIN_MP3_BYTES:
        msg = (r.stderr or r.stdout or "").strip()
        raise RuntimeError(f"무음 MP3 생성 실패: {msg or r.returncode}")


def prepend_silence_mp3(mp3_path: Path, silence_sec: float) -> None:
    """MP3 앞에 무음을 붙입니다 (파트 첫 줄 선행 쉼·호흡용).

    lavfi+concat 는 샘플레이트/채널 불일치로 Windows에서 빈 stderr·큰 exit code로
    실패하는 경우가 있어 ``adelay`` 단일 입력 필터를 우선 사용합니다.
    """
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError(
            "ffmpeg 가 필요합니다. 선행 쉼 처리를 위해 PATH에 ffmpeg 를 넣으세요."
        )
    silence_sec = max(0.05, min(3.0, float(silence_sec)))
    delay_ms = max(50, int(round(silence_sec * 1000)))
    src = Path(mp3_path).resolve()
    if not src.is_file() or src.stat().st_size < _MIN_MP3_BYTES:
        raise RuntimeError(
            f"선행 무음 대상 MP3가 비어 있거나 없습니다 "
            f"({src.stat().st_size if src.is_file() else 0} bytes): {src}"
        )
    tmp = src.with_suffix(".prepend_tmp.mp3")
    kw: dict = dict(
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    if sys.platform == "win32":
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]

    # 채널 수와 무관하게 동작: all=1 로 전 채널 동일 delay
    af = (
        f"adelay=delays={delay_ms}:all=1,"
        f"aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=mono"
    )
    r = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(src),
            "-af",
            af,
            *_FFMPEG_MP3_ENCODE_ARGS,
            str(tmp),
        ],
        **kw,
    )
    if r.returncode != 0 or not tmp.is_file() or tmp.stat().st_size < _MIN_MP3_BYTES:
        # 폴백: lavfi 무음 + aformat 정규화 후 concat
        r2 = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-t",
                f"{silence_sec:.3f}",
                "-i",
                "anullsrc=r=44100:cl=mono",
                "-i",
                str(src),
                "-filter_complex",
                (
                    "[0:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=mono[s];"
                    "[1:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=mono[m];"
                    "[s][m]concat=n=2:v=0:a=1[out]"
                ),
                "-map",
                "[out]",
                *_FFMPEG_MP3_ENCODE_ARGS,
                str(tmp),
            ],
            **kw,
        )
        if r2.returncode != 0 or not tmp.is_file() or tmp.stat().st_size < _MIN_MP3_BYTES:
            msg = (r2.stderr or r.stderr or r2.stdout or r.stdout or "").strip()
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise RuntimeError(
                f"선행 무음 삽입 실패: {msg or f'exit adelay={r.returncode} concat={r2.returncode}'}"
            )
    tmp.replace(src)


def append_silence_mp3(mp3_path: Path, silence_sec: float) -> None:
    """MP3 끝에 무음을 붙입니다.

    ``apad`` 단독 재인코딩은 짧은 클립에서 끝 음절을 깎는 경우가 있어
    본편+무음 concat 을 우선합니다.
    """
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError(
            "ffmpeg 가 필요합니다. 후행 쉼 처리를 위해 PATH에 ffmpeg 를 넣으세요."
        )
    silence_sec = max(0.05, min(3.0, float(silence_sec)))
    src = Path(mp3_path).resolve()
    if not src.is_file() or src.stat().st_size < _MIN_MP3_BYTES:
        raise RuntimeError(
            f"후행 무음 대상 MP3가 비어 있거나 없습니다 "
            f"({src.stat().st_size if src.is_file() else 0} bytes): {src}"
        )
    tmp = src.with_suffix(".append_tmp.mp3")
    kw: dict = dict(
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    if sys.platform == "win32":
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]

    # 우선: 본편 + lavfi 무음 concat (끝 잘림 적음)
    r = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(src),
            "-f",
            "lavfi",
            "-t",
            f"{silence_sec:.3f}",
            "-i",
            "anullsrc=r=44100:cl=mono",
            "-filter_complex",
            (
                "[0:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=mono,"
                "apad=pad_dur=0.05[m];"
                "[1:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=mono[s];"
                "[m][s]concat=n=2:v=0:a=1[out]"
            ),
            "-map",
            "[out]",
            *_FFMPEG_MP3_ENCODE_ARGS,
            str(tmp),
        ],
        **kw,
    )
    if r.returncode != 0 or not tmp.is_file() or tmp.stat().st_size < _MIN_MP3_BYTES:
        # 폴백: apad
        af = (
            f"apad=pad_dur={silence_sec + 0.05:.3f},"
            f"aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=mono"
        )
        r2 = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(src),
                "-af",
                af,
                *_FFMPEG_MP3_ENCODE_ARGS,
                str(tmp),
            ],
            **kw,
        )
        if r2.returncode != 0 or not tmp.is_file() or tmp.stat().st_size < _MIN_MP3_BYTES:
            msg = (r2.stderr or r.stderr or r2.stdout or r.stdout or "").strip()
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise RuntimeError(
                f"후행 무음 삽입 실패: {msg or f'exit concat={r.returncode} apad={r2.returncode}'}"
            )
    tmp.replace(src)


def concat_mp3_files(parts: list[bytes], out_path: str) -> None:
    """바이너리 이어붙이기 (ffmpeg 없을 때 대안)."""
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("wb") as w:
        for blob in parts:
            w.write(blob)


def concat_mp3_files_binary_from_paths(
    segment_paths: list[Path],
    out_path: Path,
    *,
    chunk_size: int = 1024 * 1024,
) -> None:
    """MP3 파일들을 순서대로 바이트 스트림으로 이어붙입니다 (ffmpeg 실패 시 폴백)."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if not segment_paths:
        raise ValueError("병합할 파일이 없습니다.")
    with out_path.open("wb") as w:
        for sp in segment_paths:
            p = Path(sp)
            if not p.is_file():
                raise FileNotFoundError(str(p))
            with p.open("rb") as r:
                while True:
                    chunk = r.read(chunk_size)
                    if not chunk:
                        break
                    w.write(chunk)


def _write_ffmpeg_concat_list(segment_paths: list[Path], out_path: Path) -> Path:
    """concat demuxer용 filelist.txt 경로를 반환합니다 (호출부에서 삭제)."""
    import tempfile

    out_path = Path(out_path)
    out_dir = out_path.parent.resolve()
    lines: list[str] = []
    for sp in segment_paths:
        sp = Path(sp).resolve()
        if not sp.is_file():
            raise FileNotFoundError(str(sp))
        try:
            rel = sp.relative_to(out_dir)
            esc = rel.as_posix().replace("'", "'\\''")
        except ValueError:
            esc = sp.as_posix().replace("'", "'\\''")
        lines.append(f"file '{esc}'")

    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".txt",
        delete=False,
        encoding="utf-8",
        newline="\n",
        dir=str(out_dir),
    ) as tf:
        tf.write("\n".join(lines) + "\n")
        return Path(tf.name)


def concat_mp3_files_ffmpeg(segment_paths: list[Path], out_path: Path) -> None:
    """ffmpeg concat + libmp3lame 재인코딩으로 MP3를 병합합니다.

    `-c copy`는 MP3 경계에서 DTS 비단조·중간 ID3로 클릭/길이 어긋남이 날 수 있어
    처음부터 디코드 후 한 번에 인코딩합니다. 실패 시 RuntimeError (상위에서 바이너리 폴백).
    """
    import subprocess
    import sys

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if not segment_paths:
        raise ValueError("병합할 세그먼트가 없습니다.")

    list_path = _write_ffmpeg_concat_list(segment_paths, out_path)
    kw: dict = dict(capture_output=True, text=True, timeout=3600)
    if sys.platform == "win32":
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]

    try:
        r = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(list_path),
                *_FFMPEG_MP3_ENCODE_ARGS,
                str(out_path),
            ],
            **kw,
        )
        if r.returncode != 0:
            msg = (r.stderr or r.stdout or "").strip()
            raise RuntimeError(f"ffmpeg 병합 실패: {msg or 'exit ' + str(r.returncode)}")
    finally:
        try:
            list_path.unlink(missing_ok=True)
        except OSError:
            pass


def ffprobe_duration_sec(mp3_path: Path) -> float:
    """MP3 duration seconds; 0 on failure."""
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        return 0.0
    kw: dict = dict(capture_output=True, text=True, timeout=60)
    if sys.platform == "win32":
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    try:
        r = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(mp3_path),
            ],
            **kw,
        )
        if r.returncode != 0:
            return 0.0
        return max(0.0, float((r.stdout or "").strip() or 0))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return 0.0


def synthesize_sound_effect_mp3(
    api_key: str,
    text: str,
    *,
    duration_seconds: float | None = None,
    loop: bool = False,
    prompt_influence: float = 0.35,
    timeout: int = 180,
    retries: int = 3,
) -> bytes:
    """ElevenLabs Sound Effects — POST /v1/sound-generation."""
    prompt = (text or "").strip()
    if not prompt:
        raise ValueError("SFX prompt is empty.")
    path = "/v1/sound-generation?output_format=mp3_44100_128"
    body: dict = {
        "text": prompt,
        "model_id": "eleven_text_to_sound_v2",
        "prompt_influence": max(0.0, min(1.0, float(prompt_influence))),
        "loop": bool(loop),
    }
    if duration_seconds is not None:
        dur = float(duration_seconds)
        if dur < 0.5:
            dur = 0.5
        if dur > 30.0:
            dur = 30.0
        body["duration_seconds"] = dur
    payload = json.dumps(body, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "audio/mpeg",
        "Content-Length": str(len(payload)),
        "Connection": "close",
    }
    last_err: Exception | None = None
    attempts = max(1, int(retries))
    for attempt in range(1, attempts + 1):
        ctx = ssl.create_default_context()
        conn = http.client.HTTPSConnection(DEFAULT_HOST, timeout=timeout, context=ctx)
        try:
            conn.request("POST", path, body=payload, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            if resp.status == 429 or resp.status >= 500:
                err = data.decode("utf-8", errors="replace").strip()
                last_err = RuntimeError(
                    f"ElevenLabs SFX error {resp.status}: {err or '(empty)'}"
                )
            elif resp.status >= 400:
                err = data.decode("utf-8", errors="replace")
                raise RuntimeError(f"ElevenLabs SFX error {resp.status}: {err}")
            elif not _looks_like_mp3(data):
                preview = data[:120].decode("utf-8", errors="replace").strip()
                last_err = RuntimeError(
                    f"ElevenLabs SFX bad MP3 ({len(data)} bytes)"
                    + (f": {preview}" if preview else "")
                )
            else:
                return data
        except (TimeoutError, OSError, http.client.HTTPException) as e:
            last_err = e
        finally:
            try:
                conn.close()
            except Exception:
                pass
        if attempt < attempts:
            time.sleep(min(8.0, 1.5 * attempt))
    raise RuntimeError(f"SFX failed ({attempts} tries): {last_err}") from last_err


def _db_to_linear(db: float) -> float:
    return float(10 ** (float(db) / 20.0))


def mix_speech_over_bed(
    speech_mp3: Path,
    bed_mp3: Path,
    dest: Path,
    *,
    speech_db: float = -4.0,
    bed_db: float = -20.0,
) -> None:
    """Mix crowd bed under speech MP3; output duration follows speech."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("ffmpeg required for mob crowd mix.")
    speech = Path(speech_mp3)
    bed = Path(bed_mp3)
    out = Path(dest)
    if not speech.is_file():
        raise FileNotFoundError(str(speech))
    if not bed.is_file():
        raise FileNotFoundError(str(bed))
    out.parent.mkdir(parents=True, exist_ok=True)
    # 믹스 전 말미 무음 제거 — bed가 빈 말미를 채우지 않게
    trim_trailing_silence_mp3(speech, threshold_db=-45.0)
    v_sp = _db_to_linear(speech_db)
    v_bd = _db_to_linear(bed_db)
    dur = ffprobe_duration_sec(speech)
    fade = 0.1
    if dur > 0.25:
        fade = min(0.12, max(0.06, dur * 0.04))
        fade_st = max(0.0, dur - fade)
        fade_f = f";[mx]afade=t=out:st={fade_st:.3f}:d={fade:.3f}[aout]"
        mix_label = "[mx]"
    else:
        fade_f = ";[mx]anull[aout]"
        mix_label = "[mx]"
    fc = (
        f"[0:a]volume={v_sp:.6f}[sp];"
        f"[1:a]volume={v_bd:.6f}[bd];"
        f"[sp][bd]amix=inputs=2:duration=first:dropout_transition=0:normalize=0{mix_label}"
        f"{fade_f}"
    )
    kw: dict = dict(capture_output=True, text=True, timeout=300)
    if sys.platform == "win32":
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    tmp = out.with_suffix(".mobmix.tmp.mp3")
    try:
        r = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(speech),
                "-stream_loop",
                "-1",
                "-i",
                str(bed),
                "-filter_complex",
                fc,
                "-map",
                "[aout]",
                *_FFMPEG_MP3_ENCODE_ARGS,
                str(tmp),
            ],
            **kw,
        )
        if r.returncode != 0:
            msg = (r.stderr or r.stdout or "").strip()
            raise RuntimeError(
                f"ffmpeg mob mix failed: {msg or 'exit ' + str(r.returncode)}"
            )
        tmp.replace(out)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass

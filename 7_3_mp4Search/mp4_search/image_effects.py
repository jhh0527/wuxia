# -*- coding: utf-8 -*-
"""PNG 오버레이 효과 — 합성 시 줌인·줌아웃."""

from __future__ import annotations

import math

PNG_EFFECT_FIXED = "fixed"
PNG_EFFECT_ZOOM_IN = "zoom_in"
PNG_EFFECT_ZOOM_OUT = "zoom_out"

PNG_EFFECT_OPTIONS: tuple[tuple[str, str], ...] = (
    (PNG_EFFECT_FIXED, "고정"),
    (PNG_EFFECT_ZOOM_IN, "줌인"),
    (PNG_EFFECT_ZOOM_OUT, "줌아웃"),
)

PNG_EFFECT_LABELS: dict[str, str] = {k: v for k, v in PNG_EFFECT_OPTIONS}
PNG_EFFECT_BY_LABEL: dict[str, str] = {v: k for k, v in PNG_EFFECT_OPTIONS}
PNG_EFFECT_LABELS_LIST: tuple[str, ...] = tuple(v for _, v in PNG_EFFECT_OPTIONS)

# zoompan (x,y) 양자화 완화 — 4_1_video motion.py 와 동일 원리
_MOTION_SUPERSAMPLE = 3
# 구간 전체에 걸쳐 아주 천천히 (최대 8% 확대·축소)
_ZOOM_DELTA = 0.08
_ZOOM_MAX = 1.0 + _ZOOM_DELTA


# SRT_000(제목 카드)은 줌 없이 항상 고정
FIXED_ONLY_ASSET_KEYS: frozenset[int] = frozenset({0})


def normalize_png_effect(value: str | None) -> str:
    v = (value or PNG_EFFECT_FIXED).strip().lower()
    if v in PNG_EFFECT_LABELS:
        return v
    if v in PNG_EFFECT_BY_LABEL:
        return PNG_EFFECT_BY_LABEL[v]
    return PNG_EFFECT_FIXED


def asset_effect_locked(asset_number: int | None) -> bool:
    """효과를 고정으로만 쓰는 자산 번호인지 (``SRT_000`` 제목 카드)."""
    if asset_number is None:
        return False
    return int(asset_number) in FIXED_ONLY_ASSET_KEYS


def png_effect_for_asset(asset_number: int | None, effect: str | None) -> str:
    """자산 번호에 허용된 효과. 고정 자산이면 항상 ``fixed``."""
    if asset_effect_locked(asset_number):
        return PNG_EFFECT_FIXED
    return normalize_png_effect(effect)


def png_effect_label(value: str | None) -> str:
    return PNG_EFFECT_LABELS.get(normalize_png_effect(value), "고정")


def static_image_input_framerate(duration_sec: float) -> str:
    """정지 1장 + zoompan — 입력 fps 를 1/구간길이 로 (사이클 리셋 방지)."""
    dur = max(float(duration_sec), 1e-6)
    rate = 1.0 / dur
    s = f"{rate:.12f}".rstrip("0").rstrip(".")
    return s if s else "1"


def _overlay_base_scale(w: int, h: int) -> str:
    return (
        f"[0:v]scale={w}:{h}:force_original_aspect_ratio=increase,"
        f"crop={w}:{h},setsar=1[base];"
    )


def _overlay_image_scale(w: int, h: int) -> str:
    return (
        f"[1:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black@0,setsar=1[img];"
    )


def _zoom_vf_chain(
    w: int,
    h: int,
    fps: int,
    duration_sec: float,
    effect: str,
    *,
    motion_span_sec: float | None = None,
    motion_phase_sec: float | None = None,
) -> str:
    """선형 zoompan + supersample — 4_1_video motion.py 와 동일 패턴."""
    fps = max(1, int(fps))
    d_out = max(1, int(math.ceil(max(0.2, float(duration_sec)) * fps)))
    use_span = (
        motion_span_sec is not None
        and motion_phase_sec is not None
        and float(motion_span_sec) > 1e-6
    )
    if use_span:
        d_run = max(1, int(math.ceil(float(motion_span_sec) * fps)))
        dm_run = max(1, d_run - 1)
        off = int(round(float(motion_phase_sec) * fps))
        if off < 0:
            off = 0
        max_off = max(0, d_run - d_out)
        if off > max_off:
            off = max_off
        d = d_run
        dm = dm_run
        on_e = f"(on+{off})"
    else:
        d = d_out
        dm = max(1, d - 1)
        on_e = "on"

    ss = max(1, int(_MOTION_SUPERSAMPLE))
    ow = w * ss
    oh = h * ss
    sw = max(int(ow * 1.55), ow + 32)
    sh = max(int(oh * 1.55), oh + 32)
    if sw * oh >= sh * ow:
        sh = int(round(sw * oh / ow))
    else:
        sw = int(round(sh * ow / oh))
    sw -= sw % 2
    sh -= sh % 2
    pre = (
        f"scale={sw}:{sh}:force_original_aspect_ratio=increase:flags=lanczos,"
        f"crop={sw}:{sh},setsar=1"
    )

    zmax = f"{_ZOOM_MAX:.4f}".rstrip("0").rstrip(".")
    zdelta = f"{_ZOOM_DELTA:.4f}".rstrip("0").rstrip(".")
    if effect == PNG_EFFECT_ZOOM_IN:
        zexpr = f"min({zmax}\\,1+{zdelta}*{on_e}/{dm})"
    else:
        zexpr = f"max(1\\,{zmax}-{zdelta}*{on_e}/{dm})"
    xexpr = "(iw-iw/zoom)/2"
    yexpr = "(ih-ih/zoom)/2"

    zp = (
        f"zoompan=z='{zexpr}':x='{xexpr}':y='{yexpr}':"
        f"d={d}:s={ow}x{oh}:fps={fps}"
    )
    post = f"scale={w}:{h}:flags=lanczos,setsar=1"
    return f"{pre},{zp},{post}"


def image_overlay_filters(
    w: int,
    h: int,
    *,
    effect: str = PNG_EFFECT_FIXED,
    duration_sec: float = 5.0,
    fps: int = 30,
    motion_span_sec: float | None = None,
    motion_phase_sec: float | None = None,
) -> list[str]:
    w = max(16, int(w))
    h = max(16, int(h))
    effect = normalize_png_effect(effect)
    base = _overlay_base_scale(w, h)
    tail = "[base][img]overlay=0:0:format=auto,setsar=1,format=yuv420p[vout]"
    fixed = [base + _overlay_image_scale(w, h) + tail]
    if effect == PNG_EFFECT_FIXED:
        return fixed

    zoom = _zoom_vf_chain(
        w,
        h,
        fps,
        duration_sec,
        effect,
        motion_span_sec=motion_span_sec,
        motion_phase_sec=motion_phase_sec,
    )
    animated = base + f"[1:v]{zoom}[img];" + tail
    return [animated, fixed]


def image_effect_needs_loop(effect: str) -> bool:
    return normalize_png_effect(effect) != PNG_EFFECT_FIXED

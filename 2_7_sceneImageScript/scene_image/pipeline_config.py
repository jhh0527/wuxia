# -*- coding: utf-8 -*-
"""파이프라인 설정 (모델·재시도·대기) — dist/config.json."""

from __future__ import annotations

import json
import sys
from pathlib import Path

CONFIG_NAME = "config.json"

_DEFAULTS: dict = {
    "model": "Nano Banana Pro",
    "retry_count": 3,
    "retry_wait_sec": 30,
    "generate_timeout_sec": 120,
    "genspark_url": "https://www.genspark.ai/ai_image",
}


def config_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / CONFIG_NAME
    return Path(__file__).resolve().parents[1] / "dist" / CONFIG_NAME


def load_pipeline_config() -> dict:
    p = config_path()
    out = dict(_DEFAULTS)
    if not p.is_file():
        save_pipeline_config(out)
        return out
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return out
    if not isinstance(data, dict):
        return out
    if isinstance(data.get("model"), str) and data["model"].strip():
        out["model"] = data["model"].strip()
    for key in ("retry_count", "retry_wait_sec", "generate_timeout_sec"):
        v = data.get(key)
        if isinstance(v, (int, float)) and int(v) > 0:
            out[key] = int(v)
        elif isinstance(v, str) and v.strip().isdigit():
            out[key] = int(v.strip())
    if isinstance(data.get("genspark_url"), str) and data["genspark_url"].strip():
        out["genspark_url"] = data["genspark_url"].strip()
    return out


def save_pipeline_config(cfg: dict | None = None) -> Path:
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    data = dict(_DEFAULTS)
    if cfg:
        data.update(cfg)
    p.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return p


def model_name_variants(model: str) -> tuple[str, ...]:
    m = (model or "Nano Banana Pro").strip()
    variants = [m]
    # 흔한 표기 변형
    low = m.lower()
    if "banana" in low:
        variants.extend(
            [
                "Nano Banana Pro",
                "Nano banana pro",
                "nano banana pro",
                "NanoBanana Pro",
                "NanoBananaPro",
                "Banana Pro",
            ]
        )
    # 중복 제거, 순서 유지
    seen: set[str] = set()
    out: list[str] = []
    for v in variants:
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return tuple(out)

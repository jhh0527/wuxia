# -*- coding: utf-8 -*-
"""소설별 Character Bible(JSON) 로드 · CharacterRegistry."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

_META_RE = re.compile(
    r"(?im)^[ \t]*[-*]?\s*character_data\s*[:：]\s*(.+?)\s*$"
)
_DATA_BLOCK_RE = re.compile(
    r">>>>>>>>\s*BEGIN_CHARACTER_DATA\s*>>>>>>>>\s*(.*?)\s*"
    r"<<<<<<<<\s*END_CHARACTER_DATA\s*<<<<<<<<",
    re.DOTALL | re.IGNORECASE,
)


@dataclass
class PatternRule:
    pattern: re.Pattern[str]
    value: str


@dataclass
class LookVariant:
    pattern: re.Pattern[str]
    look: str
    skip_anchor: bool = False


@dataclass
class CharacterDef:
    id: str
    display_name: str
    detect: tuple[str, ...]
    look: str = ""
    face: str = ""
    look_template: str = ""
    look_by_age: dict[str, str] = field(default_factory=dict)
    default_age: int = 19
    default_expression: str = ""
    states: dict[str, str] = field(default_factory=dict)
    state_rules: list[PatternRule] = field(default_factory=list)
    age_rules: list[tuple[re.Pattern[str], int]] = field(default_factory=list)
    emotion_rules: list[PatternRule] = field(default_factory=list)
    look_variants: list[LookVariant] = field(default_factory=list)
    anchor: str = ""
    contrast: str = ""
    stateful: bool = False

    @property
    def has_states(self) -> bool:
        return self.stateful or bool(self.states)


@dataclass
class CharacterRegistry:
    """소설별 인물 LOOK·탐지·상태 규칙."""

    title: str = ""
    protagonist_id: str = ""
    default_states: dict[str, str] = field(default_factory=dict)
    characters: dict[str, CharacterDef] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)
    source_path: str = ""

    def get(self, cid: str) -> CharacterDef | None:
        return self.characters.get(cid)

    def stateful_ids(self) -> set[str]:
        return {c.id for c in self.characters.values() if c.has_states}

    def detect_present(self, dialogue: str, scene_prompt: str | None = None) -> list[str]:
        text = f"{dialogue or ''}\n{scene_prompt or ''}"
        found: list[str] = []
        low = text.lower()
        for cid in self.order:
            ch = self.characters[cid]
            for n in ch.detect:
                if n.lower() in low or n in text:
                    if cid not in found:
                        found.append(cid)
                    break
        if not found and self.protagonist_id:
            found.append(self.protagonist_id)
        return found

    def build_contrast_clause(self, present: list[str]) -> str:
        ids = [cid for cid in present if (self.get(cid) and self.get(cid).contrast)]
        if len(ids) < 2:
            return ""
        parts = [
            f"{self.characters[cid].display_name}: {self.characters[cid].contrast}"
            for cid in ids
        ]
        return (
            "Distinct faces side by side — do NOT merge into one generic pretty boy: "
            + "; ".join(parts)
            + ". Each character must keep their unique Face Identity from Character Bible."
        )


def _compile_flags(pat: str) -> re.Pattern[str]:
    return re.compile(pat, re.I)


def _parse_character(raw: dict[str, Any]) -> CharacterDef | None:
    cid = str(raw.get("id") or "").strip()
    if not cid:
        return None
    detect = raw.get("detect") or []
    if not isinstance(detect, list):
        detect = [str(detect)]
    names = tuple(str(x) for x in detect if str(x).strip())

    state_rules: list[PatternRule] = []
    for rule in raw.get("state_rules") or []:
        if not isinstance(rule, dict):
            continue
        pat = str(rule.get("pattern") or "").strip()
        st = str(rule.get("state") or "").strip()
        if pat and st:
            state_rules.append(PatternRule(_compile_flags(pat), st))

    age_rules: list[tuple[re.Pattern[str], int]] = []
    for rule in raw.get("age_rules") or []:
        if not isinstance(rule, dict):
            continue
        pat = str(rule.get("pattern") or "").strip()
        try:
            age = int(rule.get("age"))
        except (TypeError, ValueError):
            continue
        if pat:
            age_rules.append((_compile_flags(pat), age))

    emotion_rules: list[PatternRule] = []
    for rule in raw.get("emotion_rules") or []:
        if not isinstance(rule, dict):
            continue
        pat = str(rule.get("pattern") or "").strip()
        expr = str(rule.get("expression") or "").strip()
        if pat and expr:
            emotion_rules.append(PatternRule(_compile_flags(pat), expr))

    look_variants: list[LookVariant] = []
    for rule in raw.get("look_variants") or []:
        if not isinstance(rule, dict):
            continue
        pat = str(rule.get("pattern") or "").strip()
        look = str(rule.get("look") or "").strip()
        if pat and look:
            look_variants.append(
                LookVariant(
                    _compile_flags(pat),
                    look,
                    skip_anchor=bool(rule.get("skip_anchor")),
                )
            )

    states_raw = raw.get("states") or {}
    states = (
        {str(k): str(v) for k, v in states_raw.items()}
        if isinstance(states_raw, dict)
        else {}
    )
    look_by_age_raw = raw.get("look_by_age") or {}
    look_by_age = (
        {str(k): str(v) for k, v in look_by_age_raw.items()}
        if isinstance(look_by_age_raw, dict)
        else {}
    )
    try:
        default_age = int(raw.get("default_age", 19))
    except (TypeError, ValueError):
        default_age = 19

    return CharacterDef(
        id=cid,
        display_name=str(raw.get("display_name") or cid).strip(),
        detect=names,
        look=str(raw.get("look") or "").strip(),
        face=str(raw.get("face") or "").strip(),
        look_template=str(raw.get("look_template") or "").strip(),
        look_by_age=look_by_age,
        default_age=default_age,
        default_expression=str(raw.get("default_expression") or "").strip(),
        states=states,
        state_rules=state_rules,
        age_rules=age_rules,
        emotion_rules=emotion_rules,
        look_variants=look_variants,
        anchor=str(raw.get("anchor") or "").strip(),
        contrast=str(raw.get("contrast") or "").strip(),
        stateful=bool(raw.get("stateful")) or bool(states),
    )


def registry_from_dict(data: dict[str, Any], *, source_path: str = "") -> CharacterRegistry:
    chars: dict[str, CharacterDef] = {}
    order: list[str] = []
    for raw in data.get("characters") or []:
        if not isinstance(raw, dict):
            continue
        ch = _parse_character(raw)
        if ch is None or ch.id in chars:
            continue
        chars[ch.id] = ch
        order.append(ch.id)
    default_states = data.get("default_states") or {}
    if not isinstance(default_states, dict):
        default_states = {}
    ds = {str(k): str(v) for k, v in default_states.items()}
    protagonist = str(data.get("protagonist_id") or "").strip()
    if not protagonist and order:
        protagonist = order[0]
    if not ds and protagonist:
        ds = {protagonist: "base"}
    return CharacterRegistry(
        title=str(data.get("title") or "").strip(),
        protagonist_id=protagonist,
        default_states=ds,
        characters=chars,
        order=order,
        source_path=source_path,
    )


def load_registry_file(path: Path | str) -> CharacterRegistry | None:
    p = Path(path)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return registry_from_dict(data, source_path=str(p.resolve()))


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except OSError:
        return ""


def character_data_meta(prompt_path: Path | str | None) -> str | None:
    """지침 파일에서 ``character_data: …`` 상대/절대 경로 추출."""
    if not prompt_path:
        return None
    p = Path(prompt_path)
    if not p.is_file():
        return None
    text = _read_text(p)
    m = _META_RE.search(text)
    if not m:
        return None
    val = m.group(1).strip().strip("\"'`")
    return val or None


def registry_from_prompt_embedded(prompt_path: Path | str | None) -> CharacterRegistry | None:
    """지침 파일 안 BEGIN_CHARACTER_DATA ~ END 블록 JSON."""
    if not prompt_path:
        return None
    p = Path(prompt_path)
    if not p.is_file():
        return None
    m = _DATA_BLOCK_RE.search(_read_text(p))
    if not m:
        return None
    try:
        data = json.loads(m.group(1).strip())
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return registry_from_dict(data, source_path=f"{p.resolve()}#CHARACTER_DATA")


def module_default_characters_path() -> Path:
    """소스 ``md/characters.json`` · PyInstaller 번들·exe 옆도 탐색."""
    import sys

    here = Path(__file__).resolve().parent
    candidates = [here.parent / "md" / "characters.json"]
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass) / "md" / "characters.json")
        candidates.append(Path(sys.executable).resolve().parent / "md" / "characters.json")
    for cand in candidates:
        if cand.is_file():
            return cand
    return candidates[0]


def resolve_characters_path(
    *,
    prompt_path: Path | str | None = None,
    png_dir: Path | str | None = None,
    explicit: Path | str | None = None,
) -> Path | None:
    """characters.json 탐색 순서.

    1) explicit
    2) 지침 ``character_data:`` 메타 (prompt 기준 상대경로)
    3) prompt 폴더 ``characters.json``
    4) png_dir 상위로 ``art/characters.json`` / ``characters.json``
    5) 모듈 ``md/characters.json``
    """
    if explicit:
        ep = Path(explicit).expanduser()
        if ep.is_file():
            return ep.resolve()

    pp = Path(prompt_path).expanduser() if prompt_path else None
    if pp and pp.is_file():
        meta = character_data_meta(pp)
        if meta:
            candidates = [
                Path(meta) if Path(meta).is_absolute() else pp.parent / meta,
                pp.parent / Path(meta).name,
            ]
            for cand in candidates:
                try:
                    if cand.is_file():
                        return cand.resolve()
                except OSError:
                    pass
        emb = registry_from_prompt_embedded(pp)
        if emb is not None:
            # 임베디드만 있고 파일 없음 — 가상 경로 표시용 None 유지
            # 호출측에서 embedded 우선 로드
            pass
        same = pp.parent / "characters.json"
        if same.is_file():
            return same.resolve()

    if png_dir:
        cur = Path(png_dir).expanduser()
        try:
            cur = cur.resolve()
        except OSError:
            pass
        for _ in range(8):
            for rel in ("art/characters.json", "characters.json"):
                cand = cur / rel
                if cand.is_file():
                    return cand.resolve()
            parent = cur.parent
            if parent == cur:
                break
            cur = parent

    default = module_default_characters_path()
    if default.is_file():
        return default.resolve()
    return None


@lru_cache(maxsize=8)
def _cached_load(path_str: str, mtime_ns: int) -> CharacterRegistry | None:
    del mtime_ns
    return load_registry_file(path_str)


def clear_registry_cache() -> None:
    """characters.json 재로드를 위해 LRU 캐시 비우기."""
    _cached_load.cache_clear()


def get_registry(
    *,
    prompt_path: Path | str | None = None,
    png_dir: Path | str | None = None,
    explicit: Path | str | None = None,
) -> CharacterRegistry:
    """사용 가능한 CharacterRegistry. 없으면 빈 레지스트리."""
    emb = registry_from_prompt_embedded(prompt_path)
    if emb is not None and emb.characters:
        return emb

    path = resolve_characters_path(
        prompt_path=prompt_path, png_dir=png_dir, explicit=explicit
    )
    if path is not None:
        try:
            mtime = path.stat().st_mtime_ns
        except OSError:
            mtime = 0
        reg = _cached_load(str(path), mtime)
        if reg is not None and reg.characters:
            return reg

    return CharacterRegistry()

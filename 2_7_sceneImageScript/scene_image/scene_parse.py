# -*- coding: utf-8 -*-
"""씬 스크립트 파싱 — ``SRT_XXX: prompt``."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_SCENE_RE = re.compile(
    r"^\s*SRT[_\s-]?(\d{1,6})\s*:\s*(.+?)\s*$",
    re.IGNORECASE | re.DOTALL,
)
_SCENE_START_RE = re.compile(r"^\s*SRT[_\s-]?(\d{1,6})\s*:\s*", re.IGNORECASE)
_SRT_TS = re.compile(r"^(\d{2}):(\d{2}):(\d{2})[,.](\d{1,3})$")
_SRT_ARROW = re.compile(
    r"(\d{2}:\d{2}:\d{2}[,.]\d{1,3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,.]\d{1,3})"
)
_SEC_SPLIT_RE = re.compile(r"[,;\s]+")
_SEC_RANGE_RE = re.compile(r"^(\d+)\s*[~～\-]\s*(\d+)?$")
_CHAPTER_TITLE_RE = re.compile(r"^제?\s*\d+\s*장\b")
# 20초 격자 외 항상 생성할 씬 (예: SRT_005 = 5초 · 제목 카드 직후 첫 본문)
MANDATORY_EXTRA_SCENE_SECS: tuple[int, ...] = (5,)
OPENING_BRIDGE_SCENE_SEC = 5


def parse_sec_selection(
    text: str,
    available_secs: list[int] | None = None,
) -> list[int]:
    """초 선택 — ``10,20`` · ``220~500`` · ``720~`` (끝까지).

    ``available_secs`` 가 있으면 실제 씬 목록에 있는 초만 반환한다.
    """
    raw = (text or "").strip()
    if not raw:
        return []
    pool = sorted({int(s) for s in (available_secs or []) if int(s) >= 0})
    max_sec = pool[-1] if pool else None
    out: list[int] = []
    seen: set[int] = set()

    def _add(sec: int) -> None:
        if sec < 0 or sec in seen:
            return
        if pool and sec not in pool:
            return
        seen.add(sec)
        out.append(sec)

    for part in _SEC_SPLIT_RE.split(raw):
        token = part.strip()
        if not token:
            continue
        m = _SEC_RANGE_RE.match(token)
        if m:
            lo = int(m.group(1))
            hi_raw = m.group(2)
            if hi_raw is not None:
                hi = int(hi_raw)
            elif max_sec is not None:
                hi = max_sec
            else:
                hi = lo
            if lo > hi:
                lo, hi = hi, lo
            if pool:
                for sec in pool:
                    if lo <= sec <= hi:
                        _add(sec)
            else:
                _add(lo)
                if hi != lo:
                    _add(hi)
            continue
        try:
            _add(int(token))
        except ValueError:
            continue
    return sorted(out)


@dataclass(frozen=True)
class SceneLine:
    sec: int
    prompt: str
    cut_kind: str = "grid"
    cut_reason: str = ""

    @property
    def label(self) -> str:
        return f"SRT_{self.sec:03d}"

    @property
    def png_name(self) -> str:
        return f"SRT_{self.sec:03d}.png"

    def list_label(self) -> str:
        tip = self.prompt[:72] + ("…" if len(self.prompt) > 72 else "")
        return f"{self.label}  |  {tip}"


def srt_png_name(sec: int) -> str:
    return f"SRT_{max(0, int(sec)):03d}.png"


REF_CHARACTERS_PNG = "ref_characters.png"


def scene_png_path(png_dir: Path, sec: int) -> Path:
    return Path(png_dir) / srt_png_name(sec)


def ref_characters_path(png_dir: Path | str) -> Path:
    return Path(png_dir) / REF_CHARACTERS_PNG


def prior_scene_candidate_secs(
    srt_sec: int,
    *,
    scene_secs: list[int] | None = None,
    interval_sec: int = 20,
) -> list[int]:
    """참조 후보 초 — 가까운 직전 씬부터 (내림차순).

    ``scene_secs`` 가 있으면 목록에서 ``srt_sec`` 미만 중 최근 순.
    없으면 ``SRT_{t-interval}`` (``SRT_020`` → ``SRT_005``) 한 개만.
    """
    n = int(srt_sec)
    if scene_secs:
        return [
            s
            for s in sorted({int(x) for x in scene_secs if int(x) >= 0})
            if s < n
        ][::-1]
    gap = max(1, int(interval_sec))
    if n == gap and OPENING_BRIDGE_SCENE_SEC < n:
        return [int(OPENING_BRIDGE_SCENE_SEC)]
    immediate = n - gap
    return [immediate] if immediate >= 0 else []


def previous_reference_slot_sec(
    srt_sec: int,
    *,
    interval_sec: int = 20,
    scene_secs: list[int] | None = None,
) -> int | None:
    """직전 참조 슬롯 초 (1순위 후보)."""
    cands = prior_scene_candidate_secs(
        srt_sec, scene_secs=scene_secs, interval_sec=interval_sec
    )
    return cands[0] if cands else None


def _reference_png_at_slot(
    pdir: Path,
    slot: int,
    *,
    min_bytes: int,
    last_completed_sec: int | None,
    last_completed_path: Path | str | None,
) -> Path | None:
    expect_name = srt_png_name(slot)
    if last_completed_sec is not None and int(last_completed_sec) == slot:
        lp = Path(last_completed_path) if last_completed_path else None
        if lp and lp.is_file() and lp.name == expect_name:
            try:
                if lp.stat().st_size >= int(min_bytes):
                    return lp.resolve()
            except OSError:
                pass
    if png_already_exists(pdir, slot, min_bytes=min_bytes):
        p = scene_png_path(pdir, slot)
        if p.name == expect_name:
            return p
    return None


def find_previous_reference_png(
    png_dir: Path | str,
    srt_sec: int,
    *,
    interval_sec: int = 20,
    prior_secs: list[int] | None = None,
    min_bytes: int = 512,
    last_completed_sec: int | None = None,
    last_completed_path: Path | str | None = None,
    scene_secs: list[int] | None = None,
) -> Path | None:
    """직전 참조 PNG — ``scene_secs`` 있으면 가까운 직전 씬부터 탐색."""
    del prior_secs
    pdir = Path(png_dir)
    for slot in prior_scene_candidate_secs(
        srt_sec, scene_secs=scene_secs, interval_sec=interval_sec
    ):
        path = _reference_png_at_slot(
            pdir,
            slot,
            min_bytes=min_bytes,
            last_completed_sec=last_completed_sec,
            last_completed_path=last_completed_path,
        )
        if path is not None:
            return path
    return None


def reference_png_path_ok(
    attach_ref_path: Path,
    srt_sec: int,
    *,
    interval_sec: int = 20,
    scene_secs: list[int] | None = None,
) -> bool:
    """첨부 파일이 허용된 직전 참조 슬롯인지."""
    m = re.match(r"SRT_(\d+)\.png$", attach_ref_path.name, re.IGNORECASE)
    if not m:
        return False
    ref_sec = int(m.group(1))
    cands = prior_scene_candidate_secs(
        srt_sec, scene_secs=scene_secs, interval_sec=interval_sec
    )
    return ref_sec in cands


def resolve_strict_reference_png(
    png_dir: Path | str,
    srt_sec: int,
    *,
    interval_sec: int = 20,
    last_completed_sec: int | None = None,
    last_completed_path: Path | str | None = None,
    min_bytes: int = 512,
    scene_secs: list[int] | None = None,
) -> tuple[int | None, Path | None]:
    """직전 참조 PNG — ``scene_secs`` 있으면 가까운 직전 씬부터."""
    pdir = Path(png_dir)
    for slot in prior_scene_candidate_secs(
        srt_sec, scene_secs=scene_secs, interval_sec=interval_sec
    ):
        path = _reference_png_at_slot(
            pdir,
            slot,
            min_bytes=min_bytes,
            last_completed_sec=last_completed_sec,
            last_completed_path=last_completed_path,
        )
        if path is not None:
            return slot, path
    return None, None


def _srt_timestamp_to_sec(ts: str) -> int | None:
    m = _SRT_TS.match((ts or "").strip())
    if not m:
        return None
    h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3))
    return (h * 60 + mi) * 60 + s


def last_srt_end_sec(srt_path: str | Path | None) -> int | None:
    """대본(all.srt) 마지막 큐 종료초(반올림)."""
    if not srt_path:
        return None
    p = Path(srt_path)
    if not p.is_file():
        return None
    try:
        raw = p.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None
    last: int | None = None
    for m in _SRT_ARROW.finditer(raw):
        sec = _srt_timestamp_to_sec(m.group(2))
        if sec is None:
            continue
        if last is None or sec > last:
            last = sec
    return last


def append_tail_scene_if_needed(
    scenes: list[SceneLine],
    *,
    srt_path: str | Path | None,
    interval_sec: int = 20,
) -> list[SceneLine]:
    """마지막 씬이 종료초까지 ``interval_sec`` 이내면 ``SRT_YYY``(마지막초) 추가."""
    if not scenes:
        return list(scenes)
    end = last_srt_end_sec(srt_path)
    if end is None:
        return list(scenes)
    last_sec = max(sc.sec for sc in scenes)
    gap = end - last_sec
    if gap <= 0 or gap > max(1, int(interval_sec)):
        return list(scenes)
    if any(sc.sec == end for sc in scenes):
        return list(scenes)
    out = list(scenes)
    out.append(
        SceneLine(
            sec=int(end),
            prompt=f"last-second image at {end}s (SRT end)",
        )
    )
    return out


_AUTO_LOCATION_RE = re.compile(
    r"(객잔|주루|정자|후원|마당|뜰|방(?:\s*안)?|처소|전각|대전|연무장|"
    r"산문|산길|숲|동굴|혈굴|절벽|계곡|마을|도시|성문|다리|궁|사원)"
)
_AUTO_LOCATION_MOVE_RE = re.compile(
    r"(나서|떠나|들어|도착|옮기|향하|건너|빠져나|올라|내려|"
    r"장소가\s*바뀌|장면이\s*바뀌)"
)
_AUTO_TIMELINE_RE = re.compile(
    r"(회상|과거의\s*(?:기억|장면|모습)|전생의\s*(?:기억|장면|모습|어느)|"
    r"기억이?\s*스쳤|꿈속|꿈에서|환상(?:이|이\s*보)|눈앞에\s*과거)"
)
_AUTO_CHARACTER_EXIT_RE = re.compile(
    r"(떠나|나가|사라져|사라졌|등을\s*돌려|자리를\s*떴)"
)
_AUTO_CHARACTER_ENTRY_RE = re.compile(
    r"(나타나|나타났|다가오|들어오|걸어오|도착|"
    r"정체는|바로\s+.+(?:였다|이었다)|(?:였다|이었다))"
)


def _explicit_characters(text: str, names: tuple[str, ...]) -> set[str]:
    low = (text or "").casefold()
    return {name for name in names if name.casefold() in low}


def detect_transition_cuts(
    srt_path: str | Path | None,
    *,
    interval_sec: int = 20,
    character_names: list[str] | tuple[str, ...] | None = None,
    existing_secs: list[int] | tuple[int, ...] | None = None,
) -> dict[int, str]:
    """20초 격자 안의 의미 전환을 보강할 추가 컷 ``초 → 이유``.

    각 격자의 8~16초 지점에서 장소·시점·실명 인물 변화만 보며,
    한 격자에는 최대 한 컷을 추가한다. 격자점에 가까운 변화와
    이미 존재하는 불규칙 씬에 가까운 변화는 추가하지 않는다.
    """
    cues = parse_srt_cues(srt_path)
    if not cues:
        return {}
    gap = max(1, int(interval_sec))
    if gap < 12:
        return {}

    names = tuple(
        sorted(
            {
                re.sub(r"\s+", " ", str(name)).strip()
                for name in (character_names or [])
                if len(re.sub(r"\s+", "", str(name))) >= 2
            },
            key=len,
            reverse=True,
        )
    )
    end = max(c.end for c in cues)
    occupied = {int(sec) for sec in (existing_secs or []) if int(sec) >= 0}
    cuts: dict[int, str] = {}
    seen_before: list[set[str]] = []
    seen: set[str] = set()
    for cue in cues:
        seen_before.append(set(seen))
        seen.update(_explicit_characters(cue.text, names))

    # 제목 카드·SRT_005가 있는 첫 구간은 기존 인트로 구성을 유지한다.
    for base in range(gap, int(end) + 1, gap):
        upper = min(float(base + gap), end)
        if upper <= base:
            continue
        # 이미 수동/고정 추가 컷이 있으면 이 격자에는 자동으로 더 넣지 않는다.
        if any(base < sec < base + gap and sec % gap != 0 for sec in occupied):
            continue

        ranked: list[tuple[int, float, int, str]] = []
        for i, cue in enumerate(cues):
            offset = cue.start - float(base)
            if offset < 8.0 or offset >= float(gap - 4):
                continue
            if cue.start >= upper:
                continue
            sec = int(cue.start + 0.5)
            if sec <= base or sec >= base + gap:
                continue
            if any(
                sec - used < 8
                for used in occupied | set(cuts)
                if used < sec
            ):
                continue

            # 잘린 SRT 문장을 보완하되 다음 장면을 과하게 끌어오지 않는다.
            context_parts = [cue.text]
            for nxt in cues[i + 1 :]:
                if nxt.start >= cue.start + 6.0:
                    break
                context_parts.append(nxt.text)
            context = " ".join(context_parts)

            reason = ""
            priority = 0
            if _AUTO_LOCATION_RE.search(context) and _AUTO_LOCATION_MOVE_RE.search(context):
                reason, priority = "장소", 3
            elif _AUTO_TIMELINE_RE.search(context):
                reason, priority = "시점", 3
            elif names:
                now_chars = _explicit_characters(context, names)
                recent_chars = _explicit_characters(
                    " ".join(
                        c.text
                        for c in cues[:i]
                        if c.end > max(float(base), cue.start - float(gap))
                    ),
                    names,
                )
                new_chars = now_chars - seen_before[i]
                exits = now_chars & recent_chars
                if new_chars and _AUTO_CHARACTER_ENTRY_RE.search(context):
                    reason, priority = "인물", 2
                elif exits and _AUTO_CHARACTER_EXIT_RE.search(context):
                    reason, priority = "인물", 2
            if priority:
                ranked.append((-priority, cue.start, sec, reason))

        if ranked:
            _neg_priority, _start, sec, reason = min(ranked)
            cuts[sec] = reason
            occupied.add(sec)
    return cuts


def build_interval_scenes(
    scenes: list[SceneLine],
    *,
    srt_path: str | Path | None,
    interval_sec: int = 20,
    character_names: list[str] | tuple[str, ...] | None = None,
    auto_transition_cuts: bool = False,
) -> list[SceneLine]:
    """생성 간격으로 ``0 … 마지막초`` 씬 목록. 프롬프트에 있으면 본문 유지.

    SRT가 있으면 **간격 격자(+종료초)** 만 사용한다.
    가이드 문서의 예시 ``SRT_026:`` 처럼 격자 밖 초는 넣지 않는다.
    """
    gap = max(1, int(interval_sec))
    by_sec = {sc.sec: sc for sc in scenes}
    end = last_srt_end_sec(srt_path)
    if end is None:
        # SRT 없으면 프롬프트에 적힌 씬만
        if scenes:
            return sorted(scenes, key=lambda s: s.sec)
        return []
    secs: list[int] = list(range(0, end + 1, gap))
    for extra in MANDATORY_EXTRA_SCENE_SECS:
        if 0 <= int(extra) <= int(end):
            secs.append(int(extra))
    if not secs or secs[-1] != end:
        secs.append(int(end))
    secs = sorted(set(secs))
    transition_cuts: dict[int, str] = {}
    if auto_transition_cuts:
        transition_cuts = detect_transition_cuts(
            srt_path,
            interval_sec=gap,
            character_names=character_names,
            existing_secs=secs,
        )
        secs.extend(transition_cuts)
        secs = sorted(set(secs))
    out: list[SceneLine] = []
    for sec in secs:
        if sec in by_sec:
            out.append(by_sec[sec])
        else:
            reason = transition_cuts.get(sec, "")
            out.append(
                SceneLine(
                    sec=int(sec),
                    prompt=f"image at {sec}s",
                    cut_kind="transition" if reason else "grid",
                    cut_reason=reason,
                )
            )
    return out


def find_latest_prior_png_on_disk(
    png_dir: Path | str,
    srt_sec: int,
    *,
    min_bytes: int = 512,
) -> Path | None:
    """``srt_sec`` 보다 작은 초 중 PNG 폴더에 있는 최신 ``SRT_XXX.png``."""
    pdir = Path(png_dir)
    if not pdir.is_dir():
        return None
    best_sec = -1
    best: Path | None = None
    for p in pdir.glob("SRT_*.png"):
        m = re.match(r"^SRT_(\d+)\.png$", p.name, re.I)
        if not m:
            continue
        sec = int(m.group(1))
        if sec >= int(srt_sec) or sec <= best_sec:
            continue
        try:
            if p.stat().st_size >= int(min_bytes):
                best_sec = sec
                best = p
        except OSError:
            continue
    return best.resolve() if best else None


def png_already_exists(png_dir: Path, sec: int, *, min_bytes: int = 512) -> bool:
    """PNG 폴더에 유효한 SRT_XXX.png가 있으면 재생성하지 않음."""
    p = scene_png_path(png_dir, sec)
    try:
        return p.is_file() and p.stat().st_size >= int(min_bytes)
    except OSError:
        return False


@dataclass(frozen=True)
class SrtCue:
    start: float
    end: float
    text: str


_PLACEHOLDER_PROMPT_RE = re.compile(
    r"^(?:image at \d+s|last-second image at \d+s(?:\s*\(SRT end\))?)\s*$",
    re.IGNORECASE,
)


def _srt_timestamp_to_float(ts: str) -> float | None:
    m = _SRT_TS.match((ts or "").strip())
    if not m:
        return None
    h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3))
    ms = int((m.group(4) or "0").ljust(3, "0")[:3])
    return float((h * 60 + mi) * 60 + s) + ms / 1000.0


def detect_chapter_title_from_srt(srt_path: str | Path | None) -> str | None:
    """SRT 첫 큐가 ``제N장 …`` 형식이면 장 제목 문자열."""
    cues = parse_srt_cues(srt_path)
    if not cues:
        return None
    first = (cues[0].text or "").strip()
    if not first or not _CHAPTER_TITLE_RE.match(first):
        return None
    return first


def parse_srt_cues(srt_path: str | Path | None) -> list[SrtCue]:
    """SRT 큐 (시작·종료·대사) 목록."""
    if not srt_path:
        return []
    p = Path(srt_path)
    if not p.is_file():
        return []
    try:
        raw = p.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return []
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    cues: list[SrtCue] = []
    for block in re.split(r"\n\s*\n", raw.strip()):
        lines = [ln.strip() for ln in block.split("\n") if ln.strip()]
        if len(lines) < 2:
            continue
        arrow: str | None = None
        text_lines: list[str] = []
        for ln in lines:
            if arrow is None and "-->" in ln:
                arrow = ln
                continue
            if arrow is not None and not ln.isdigit():
                text_lines.append(ln)
        if not arrow:
            continue
        m = _SRT_ARROW.search(arrow)
        if not m:
            continue
        start = _srt_timestamp_to_float(m.group(1))
        end = _srt_timestamp_to_float(m.group(2))
        if start is None or end is None:
            continue
        text = " ".join(text_lines).strip()
        if text:
            cues.append(SrtCue(start=float(start), end=float(end), text=text))
    return cues


def _dedupe_srt_texts(parts: list[str]) -> str:
    """큐 단위 중복 제거(경계 겹침 유지하되 동일 문장 반복만 제거)."""
    out: list[str] = []
    seen: set[str] = set()
    for t in parts:
        key = re.sub(r"\s+", " ", t).strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(t.strip())
    return "\n".join(out).strip()


def _srt_texts_in_range(
    cues: list[SrtCue],
    t0: float,
    t1: float,
) -> list[str]:
    return [c.text for c in cues if c.end > t0 and c.start < t1]


def srt_dialogue_until_next_scene(
    srt_path: str | Path | None,
    sec: int,
    next_sec: int | None = None,
) -> str:
    """``sec`` 초부터 ``next_sec`` 직전까지 겹치는 SRT 대본 전체."""
    cues = parse_srt_cues(srt_path)
    if not cues:
        return ""
    t0 = float(max(0, int(sec)))
    if next_sec is not None:
        t1 = float(int(next_sec))
    else:
        end = last_srt_end_sec(srt_path)
        if end is not None:
            t1 = float(end) + 1.0
        else:
            t1 = max(c.end for c in cues) + 1.0
    return _dedupe_srt_texts(_srt_texts_in_range(cues, t0, t1))


def srt_dialogue_for_window(
    srt_path: str | Path | None,
    sec: int,
    interval_sec: int = 20,
) -> str:
    """영상 초 T 구간의 대본 — ``[T, T+interval)`` 에 겹치는 큐.

    종료초 단독 씬처럼 창이 비면 직전 ``interval`` 구간으로 한 번 더 찾는다.
    """
    cues = parse_srt_cues(srt_path)
    if not cues:
        return ""
    gap = max(1, int(interval_sec))
    t0 = float(max(0, int(sec)))
    t1 = t0 + float(gap)

    parts = _srt_texts_in_range(cues, t0, t1)
    if not parts:
        parts = _srt_texts_in_range(cues, max(0.0, t0 - float(gap)), t0)
    return _dedupe_srt_texts(parts)


def srt_context_before(
    srt_path: str | Path | None,
    sec: int,
    lookback_sec: int = 60,
) -> str:
    """현재 씬 직전 문맥 — ``[max(0, T-lookback), T)`` 대본.

    이미지에 그릴 내용이 아니라 장소·인물·인과 파악용.
    """
    cues = parse_srt_cues(srt_path)
    if not cues:
        return ""
    t1 = float(max(0, int(sec)))
    if t1 <= 0:
        return ""
    look = max(1, int(lookback_sec))
    t0 = max(0.0, t1 - float(look))
    return _dedupe_srt_texts(_srt_texts_in_range(cues, t0, t1))


_PLACE_KW = re.compile(
    r"(정자|정자각|정자閣|야외|실내|마당|뜰| courtyard|"
    r"pavilion|garden|forest|mountain|cave|temple|inn|hall|room|"
    r"차실|茶室|客棧|客店|산|林|洞|殿|堂|阁|亭|客栈|酒楼|"
    r"river|lake|cliff|valley|village|city|gate|bridge|palace|"
    r"roof|terrace|balcony|night|dawn| dusk)",
    re.I,
)


def scene_location_fingerprint(
    srt_path: str | Path | None,
    sec: int,
    *,
    scene_prompt: str | None = None,
    interval_sec: int = 20,
) -> str:
    """장소 추정 키 — 연속 동일 키면 참조 첨부 생략용."""
    dialogue = srt_dialogue_for_window(
        srt_path, sec, interval_sec=interval_sec
    )
    prompt = re.sub(r"\s+", " ", (scene_prompt or "").strip())
    blob = f"{prompt}\n{dialogue}"
    hits = sorted({m.group(0).lower() for m in _PLACE_KW.finditer(blob)})
    if hits:
        return "|".join(hits[:10])
    if len(prompt) >= 16:
        return prompt[:96].lower()
    norm_d = re.sub(r"\s+", " ", dialogue[:240]).strip().lower()
    if len(norm_d) >= 16:
        return norm_d[:96]
    return ""


def is_real_scene_prompt(prompt: str | None) -> bool:
    """격자 placeholder·가이드 예시/템플릿이 아닌 실장면 프롬프트인지."""
    p = (prompt or "").strip()
    if len(p) < 40:
        return False
    if _PLACEHOLDER_PROMPT_RE.match(p):
        return False
    if p.startswith("…") or p.startswith("..."):
        return False
    if "[§0" in p or "§0 LOOK" in p or "§0-X" in p:
        return False
    if "BEGIN_NOVEL" in p or "작성 지침" in p or "NOVEL PACK" in p:
        return False
    return True


def parse_scene_script(text: str) -> list[SceneLine]:
    """textarea 본문에서 ``SRT_XXX: …`` 씬을 추출.

    한 줄에 프롬프트가 길거나, 빈 줄로 구분된 블록도 허용.
    """
    raw = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not raw.strip():
        return []

    scenes: list[SceneLine] = []
    cur_sec: int | None = None
    cur_parts: list[str] = []

    def flush() -> None:
        nonlocal cur_sec, cur_parts
        if cur_sec is None:
            return
        prompt = " ".join(p.strip() for p in cur_parts if p.strip()).strip()
        prompt = re.sub(r"\s+", " ", prompt)
        if prompt:
            scenes.append(SceneLine(sec=int(cur_sec), prompt=prompt))
        cur_sec = None
        cur_parts = []

    for line in raw.split("\n"):
        m = _SCENE_START_RE.match(line)
        if m:
            flush()
            cur_sec = int(m.group(1))
            rest = line[m.end() :].strip()
            cur_parts = [rest] if rest else []
            continue
        if cur_sec is not None:
            if line.strip():
                cur_parts.append(line.strip())
            else:
                # 빈 줄 — 다음 SRT_ 전까지 이어붙이거나 종료
                continue
    flush()

    # 한 줄 정규식 보조 (위에서 못 잡은 경우 거의 없음)
    if not scenes:
        for m in re.finditer(
            r"SRT[_\s-]?(\d{1,6})\s*:\s*(.+?)(?=(?:\n\s*SRT[_\s-]?\d)|\Z)",
            raw,
            re.IGNORECASE | re.DOTALL,
        ):
            prompt = re.sub(r"\s+", " ", m.group(2).strip())
            if prompt:
                scenes.append(SceneLine(sec=int(m.group(1)), prompt=prompt))
    return scenes

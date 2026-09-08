# -*- coding: utf-8 -*-
"""Genspark 응답에서 dialogue JSON 추출·검증·저장."""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

# MD/샘플 안의 형식 예시 — 실제 변환본으로 오인하지 않음
_PLACEHOLDER_CHAPTERS = frozenset({"장 제목", "chapter title", "제목"})
_PLACEHOLDER_TEXTS = frozenset(
    {
        "나레이션 문장",
        "대사 내용.",
        '[tired] "대사 내용."',
        "[tired] \"대사 내용.\"",
    }
)

# CJK 한자(확장 A·본문·호환) — TTS 낭독에서 제외
_HANJA_RE = re.compile(r"[\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF]")
_EMPTY_PAREN_RE = re.compile(r"[（(]\s*[）)]")
_CHAPTER_TITLE_RE = re.compile(r"^제?\s*\d+\s*장\b")


def strip_hanja(text: str) -> str:
    """한자·빈 괄호(板 병기 등) 제거. 한글·숫자·허용 구두점은 유지."""
    s = _HANJA_RE.sub("", text or "")
    s = _EMPTY_PAREN_RE.sub("", s)
    s = re.sub(r"[ \t]{2,}", " ", s)
    return s.strip()


def ensure_chapter_title_input(data: dict[str, Any]) -> dict[str, Any]:
    """``chapter`` 를 한자 없이 정리하고, inputs[0] narrator 장 제목으로 보장."""
    result = dict(data)
    ch = strip_hanja(str(result.get("chapter") or "").strip())
    if not ch or ch in _PLACEHOLDER_CHAPTERS:
        return result
    result["chapter"] = ch

    inputs = result.get("inputs")
    if not isinstance(inputs, list):
        return result
    out: list[Any] = list(inputs)
    title_row = {"speaker": "narrator", "text": ch}

    if not out:
        result["inputs"] = [title_row]
        return result

    first = out[0] if isinstance(out[0], dict) else None
    first_tx = strip_hanja(str((first or {}).get("text") or "").strip())
    if first_tx == ch:
        if first is not None:
            row = dict(first)
            row["speaker"] = "narrator"
            row["text"] = ch
            out[0] = row
        result["inputs"] = out
        return result
    if first_tx and _CHAPTER_TITLE_RE.match(first_tx):
        row = dict(first) if first is not None else {}
        row["speaker"] = "narrator"
        row["text"] = ch
        out[0] = row
    else:
        out.insert(0, title_row)
    result["inputs"] = out
    return result

# Genspark 채팅 UI가 스크래프에 섞이는 문구
_GENSPARK_UI_NOISE_RE = re.compile(
    r"(?:"
    r"\r?\n?\s*Copy\s*(?:\r?\n)?"
    r"|"
    r"\r?\n?\s*Claude\s*Opus\s*4\s*[.\s]?6\s*"
    r"|"
    r"\r?\n?\s*Opus\s*4\s*[.\s]?6\s*"
    r"|"
    r"\r?\n?\s*Stop\s*generating\s*"
    r"|"
    r"\r?\n?\s*생성을\s*중지\s*"
    r")+",
    flags=re.IGNORECASE,
)
_GENSPARK_UI_TAIL_RE = re.compile(
    r"(?:\r?\n|\s)*(?:Copy|Claude\s*Opus\s*4\s*[.\s]?6|Opus\s*4\s*[.\s]?6)\s*$",
    flags=re.IGNORECASE,
)


def strip_genspark_ui_noise(text: str) -> str:
    """응답 본문·대사에서 Copy / 모델명 등 UI 잔여를 제거."""
    s = text or ""
    if not s:
        return ""
    prev = None
    while prev != s:
        prev = s
        s = _GENSPARK_UI_NOISE_RE.sub("", s)
        s = _GENSPARK_UI_TAIL_RE.sub("", s)
    return s.strip()


def dialogue_has_ui_noise(data: dict[str, Any]) -> bool:
    """inputs text 에 Genspark UI 문구가 남아 있으면 True."""
    inputs = data.get("inputs")
    if not isinstance(inputs, list):
        return False
    for item in inputs:
        if not isinstance(item, dict):
            continue
        t = str(item.get("text") or "")
        if re.search(r"(?i)\bCopy\b", t) and re.search(r"(?i)Claude|Opus", t):
            return True
        if re.search(r"(?i)Claude\s*Opus\s*4", t):
            return True
        if re.search(r"(?i)\nCopy\n", t):
            return True
    return False


def _text_looks_truncated(text: str) -> bool:
    """문장 중간에서 끊긴 듯한 마지막 대사."""
    t = (text or "").strip()
    if len(t) < 8:
        return False
    if t.endswith((".", "!", "?", "…", "。”", '."', "」", "』", '"', "”")):
        return False
    if re.search(r"\]\s*\"[^\"]*\"\s*$", t):
        return False
    # 한글·영문 어절 중간 절단
    if re.search(r"[가-힣A-Za-z0-9]$", t):
        return True
    return False


def strip_ui_noise_from_dialogue(data: dict[str, Any]) -> dict[str, Any]:
    """dialogue dict 의 모든 text 필드에서 UI 노이즈 제거."""
    inputs = data.get("inputs")
    if not isinstance(inputs, list):
        return data
    out: list[dict[str, Any]] = []
    for item in inputs:
        if not isinstance(item, dict):
            continue
        row = dict(item)
        row["text"] = strip_genspark_ui_noise(str(row.get("text") or ""))
        if not str(row.get("text") or "").strip():
            continue
        out.append(row)
    # 맨 끝 항목이 UI 때문에 잘린 잔여면 제거
    while out and _text_looks_truncated(str(out[-1].get("text") or "")):
        # 직전 항목이 온전하면 잘린 마지막만 버림
        if len(out) >= 2 and not _text_looks_truncated(str(out[-2].get("text") or "")):
            out.pop()
            break
        break
    result = dict(data)
    result["inputs"] = out
    return result


def _balanced_objects(text: str) -> list[str]:
    """텍스트에서 균형 잡힌 ``{…}`` 후보를 모두 찾는다."""
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        depth = 0
        in_str = False
        esc = False
        for j in range(i, n):
            ch = text[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    out.append(text[i : j + 1])
                    i = j + 1
                    break
        else:
            break
    return out


def _balanced_arrays(text: str) -> list[str]:
    """균형 잡힌 ``[…]`` 후보 (inputs 배열만 출력된 경우)."""
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] != "[":
            i += 1
            continue
        depth = 0
        in_str = False
        esc = False
        for j in range(i, n):
            ch = text[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    chunk = text[i : j + 1]
                    if '"speaker"' in chunk and '"text"' in chunk:
                        out.append(chunk)
                    i = j + 1
                    break
        else:
            break
    return out


def _normalize_for_fingerprint(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@lru_cache(maxsize=4)
def _bundled_sample_fingerprints() -> frozenset[str]:
    """md/jsonSample.json 지문 — 첨부·인라인 샘플을 결과로 쓰지 않기 위함."""
    fps: set[str] = set()
    try:
        from text_to_json.paths import json_sample_path

        sp = json_sample_path()
    except Exception:
        sp = None
    paths: list[Path] = []
    if sp is not None:
        paths.append(sp)
    here = Path(__file__).resolve().parents[1] / "md" / "jsonSample.json"
    paths.append(here)
    for p in paths:
        try:
            if not p.is_file():
                continue
            data = json.loads(p.read_text(encoding="utf-8-sig"))
            if isinstance(data, dict):
                fps.add(_normalize_for_fingerprint(data))
                ch = str(data.get("chapter") or "").strip()
                inputs = data.get("inputs")
                if ch and isinstance(inputs, list) and inputs:
                    t0 = str((inputs[0] or {}).get("text") or "")[:80]
                    t1 = str((inputs[-1] or {}).get("text") or "")[:80]
                    fps.add(f"sig:{ch}|{len(inputs)}|{t0}|{t1}")
        except (OSError, json.JSONDecodeError, TypeError, AttributeError):
            continue
    return frozenset(fps)


def is_known_sample_dialogue(data: dict[str, Any]) -> bool:
    """jsonSample.json 과 동일·동일시그니처면 True."""
    if not isinstance(data, dict):
        return False
    fps = _bundled_sample_fingerprints()
    if not fps:
        return False
    if _normalize_for_fingerprint(data) in fps:
        return True
    ch = str(data.get("chapter") or "").strip()
    inputs = data.get("inputs")
    if ch and isinstance(inputs, list) and inputs:
        try:
            t0 = str((inputs[0] or {}).get("text") or "")[:80]
            t1 = str((inputs[-1] or {}).get("text") or "")[:80]
            if f"sig:{ch}|{len(inputs)}|{t0}|{t1}" in fps:
                return True
        except (TypeError, AttributeError):
            pass
    return False


def is_placeholder_dialogue(data: dict[str, Any]) -> bool:
    """지침/샘플용 더미 JSON 여부."""
    if is_known_sample_dialogue(data):
        return True
    ch = str(data.get("chapter") or "").strip()
    if ch in _PLACEHOLDER_CHAPTERS:
        return True
    inputs = data.get("inputs")
    if not isinstance(inputs, list) or not inputs:
        return True
    texts = [
        str(it.get("text") or "").strip()
        for it in inputs
        if isinstance(it, dict)
    ]
    if not texts:
        return True
    if all(t in _PLACEHOLDER_TEXTS or t.startswith("나레이션") for t in texts):
        if len(inputs) <= 3:
            return True
    if any(t in _PLACEHOLDER_TEXTS for t in texts) and len(inputs) <= 2:
        return True
    return False


def _score_dialogue(data: dict[str, Any]) -> tuple[int, int]:
    """(inputs 수, 본문 글자 수) — 클수록 실제 변환본."""
    inputs = data.get("inputs")
    if not isinstance(inputs, list):
        return (0, 0)
    chars = 0
    for it in inputs:
        if isinstance(it, dict):
            chars += len(str(it.get("text") or ""))
    return (len(inputs), chars)


def _normalize_jsonish(text: str) -> str:
    """스마트 따옴표·트레일링 콤마 등 흔한 변형을 정리."""
    t = (
        (text or "")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\ufeff", "")
    )
    t = re.sub(r",\s*}", "}", t)
    t = re.sub(r",\s*]", "]", t)
    return t


def repair_unescaped_text_quotes(text: str) -> str:
    """``"text": "…[tag] "대사."…"`` 처럼 text 값 안의 미이스케이프 따옴표를 고친다.

    Genspark/Claude 가 dialogue JSON 을 낼 때 자주 깨지는 패턴.
    """
    s = text or ""
    out: list[str] = []
    i = 0
    n = len(s)
    while i < n:
        m = re.search(r'"text"\s*:\s*"', s[i:])
        if not m:
            out.append(s[i:])
            break
        start = i + m.end()
        out.append(s[i:start])
        j = start
        buf: list[str] = []
        while j < n:
            ch = s[j]
            if ch == "\\" and j + 1 < n:
                buf.append(ch)
                buf.append(s[j + 1])
                j += 2
                continue
            if ch == '"':
                rest = s[j + 1 : j + 24].lstrip()
                if rest.startswith("}") or rest.startswith(","):
                    out.append("".join(buf))
                    out.append('"')
                    i = j + 1
                    break
                buf.append('\\"')
                j += 1
                continue
            buf.append(ch)
            j += 1
        else:
            out.append("".join(buf))
            break
    return "".join(out)


def _wrap_inputs_list(inputs: list[Any]) -> dict[str, Any] | None:
    if not inputs or not all(isinstance(x, dict) for x in inputs):
        return None
    return {
        "chapter": "untitled",
        "voices_file": "../../../voices.json",
        "chunk_id": "A1",
        "inputs": inputs,
    }


def _try_load_dialogue(cand: str) -> dict[str, Any] | None:
    for variant in (
        cand,
        repair_unescaped_text_quotes(cand),
        _normalize_jsonish(cand),
        repair_unescaped_text_quotes(_normalize_jsonish(cand)),
    ):
        try:
            data = json.loads(variant)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if isinstance(data, dict):
            return data
        if isinstance(data, list):
            wrapped = _wrap_inputs_list(data)
            if wrapped is not None:
                return wrapped
    return None


def _sample_text_set() -> frozenset[str]:
    """jsonSample 본문 — loose 추출 시 혼입 제거용."""
    out: set[str] = set()
    try:
        from text_to_json.paths import json_sample_path

        sp = json_sample_path()
    except Exception:
        sp = None
    paths: list[Path] = []
    if sp is not None:
        paths.append(sp)
    paths.append(Path(__file__).resolve().parents[1] / "md" / "jsonSample.json")
    for p in paths:
        try:
            if not p.is_file():
                continue
            data = json.loads(p.read_text(encoding="utf-8-sig"))
            for it in data.get("inputs") or []:
                if isinstance(it, dict):
                    t = str(it.get("text") or "").strip()
                    if t:
                        out.add(t)
        except (OSError, json.JSONDecodeError, TypeError, AttributeError):
            continue
    out.update(_PLACEHOLDER_TEXTS)
    return frozenset(out)


def _extract_inputs_loose(text: str) -> list[dict[str, str]]:
    """균형 JSON 이 깨져도 ``speaker``/``text`` 쌍을 느슨하게 모은다."""
    s = _normalize_jsonish(text)
    sample_texts = _sample_text_set()
    items: list[dict[str, str]] = []
    for m in re.finditer(
        r'\{\s*"speaker"\s*:\s*"([^"]+)"\s*,\s*"text"\s*:\s*"',
        s,
    ):
        speaker = (m.group(1) or "").strip()
        j = m.end()
        buf: list[str] = []
        n = len(s)
        while j < n:
            ch = s[j]
            if ch == "\\" and j + 1 < n:
                buf.append(s[j + 1])
                j += 2
                continue
            if ch == '"':
                rest = s[j + 1 : j + 24].lstrip()
                if rest.startswith("}") or rest.startswith(","):
                    break
                buf.append('"')
                j += 1
                continue
            buf.append(ch)
            j += 1
        text_val = "".join(buf).strip()
        if not speaker or not text_val:
            continue
        if text_val in sample_texts:
            continue
        if text_val.startswith("나레이션 문장"):
            continue
        items.append({"speaker": speaker, "text": text_val})
    return items


def _rebuild_from_loose(text: str) -> str:
    """느슨 추출한 inputs 로 최소 dialogue JSON 을 재구성."""
    items = _extract_inputs_loose(text)
    if len(items) < 2:
        return ""
    chapter = ""
    m = re.search(r'"chapter"\s*:\s*"((?:\\.|[^"\\])*)"', text)
    if m:
        try:
            chapter = json.loads(f'"{m.group(1)}"')
        except (json.JSONDecodeError, TypeError, ValueError):
            chapter = m.group(1)
    if not chapter or chapter.strip() in _PLACEHOLDER_CHAPTERS:
        # 지침 예시 chapter 가 먼저 잡혀도 실제 inputs 는 살린다
        chapter = "untitled"
        for it in items:
            t = str(it.get("text") or "").strip()
            if t and not t.startswith("["):
                chapter = t[:80]
                break
    chunk = "A1"
    m2 = re.search(r'"chunk_id"\s*:\s*"((?:\\.|[^"\\])*)"', text)
    if m2:
        try:
            chunk = json.loads(f'"{m2.group(1)}"') or "A1"
        except (json.JSONDecodeError, TypeError, ValueError):
            chunk = m2.group(1) or "A1"
        if chunk.strip() in {"A1", "chunk", "id"}:
            pass
    data = {
        "chapter": chapter,
        "voices_file": "../../../voices.json",
        "chunk_id": chunk or "A1",
        "inputs": items,
    }
    if is_placeholder_dialogue(data):
        return ""
    return json.dumps(data, ensure_ascii=False)


def _candidate_ok(data: dict[str, Any]) -> bool:
    if not isinstance(data, dict):
        return False
    inputs = data.get("inputs")
    if not isinstance(inputs, list) or not inputs:
        return False
    if is_placeholder_dialogue(data):
        return False
    for item in inputs:
        if not isinstance(item, dict):
            return False
        if not str(item.get("speaker") or "").strip():
            return False
        if not str(item.get("text") or "").strip():
            return False
    return True


def extract_json_payload(raw: str) -> str:
    """응답에서 **실제** dialogue JSON 본문만 뽑는다 (샘플/지침 예시 제외)."""
    text = strip_genspark_ui_noise((raw or "").strip())
    if not text:
        return ""

    variants = [
        text,
        _normalize_jsonish(text),
        repair_unescaped_text_quotes(text),
        repair_unescaped_text_quotes(_normalize_jsonish(text)),
    ]

    candidates: list[str] = []
    for src in variants:
        for m in re.finditer(
            r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", src, flags=re.IGNORECASE
        ):
            candidates.append(m.group(1).strip())
        # 코드펜스가 안 닫힌 경우: ```json { … 끝까지
        for m in re.finditer(
            r"```(?:json)?\s*(\{[\s\S]+)", src, flags=re.IGNORECASE
        ):
            chunk = m.group(1).strip()
            if chunk.endswith("```"):
                chunk = chunk[:-3].rstrip()
            candidates.append(chunk)
        candidates.extend(_balanced_objects(src))
        candidates.extend(_balanced_arrays(src))

    best_payload = ""
    best_score = (-1, -1)
    for cand in candidates:
        data = _try_load_dialogue(cand)
        if data is None or not _candidate_ok(data):
            continue
        data = strip_ui_noise_from_dialogue(data)
        if not _candidate_ok(data):
            continue
        # UI 노이즈가 남아 있거나 끝이 잘렸으면 점수 낮춤 (생성 중 스크래프)
        score = list(_score_dialogue(data))
        if dialogue_has_ui_noise(data):
            score[0] = max(0, score[0] - 50)
        inputs = data.get("inputs") or []
        if inputs and isinstance(inputs[-1], dict):
            if _text_looks_truncated(str(inputs[-1].get("text") or "")):
                score[0] = max(0, score[0] - 20)
        score_t = (score[0], score[1])
        if score_t > best_score:
            best_score = score_t
            best_payload = json.dumps(data, ensure_ascii=False)

    if best_payload:
        return best_payload

    rebuilt = _rebuild_from_loose(text)
    if rebuilt:
        data = _try_load_dialogue(rebuilt)
        if data is not None and _candidate_ok(data):
            data = strip_ui_noise_from_dialogue(data)
            if _candidate_ok(data):
                return json.dumps(data, ensure_ascii=False)
    return ""


def parse_dialogue_json(raw: str) -> dict[str, Any]:
    """JSON 파싱 후 dialogue 형식 검증."""
    payload = extract_json_payload(raw)
    if not payload:
        try:
            data = json.loads((raw or "").strip())
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            raise ValueError("JSON 본문이 비어 있거나 샘플만 있습니다.") from e
        if isinstance(data, dict) and not is_placeholder_dialogue(data):
            payload = json.dumps(data, ensure_ascii=False)
        else:
            raise ValueError("JSON 본문이 비어 있거나 샘플만 있습니다.")
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON 파싱 실패: {e}") from e
    if not isinstance(data, dict):
        raise ValueError("최상위는 JSON 객체여야 합니다.")
    if is_placeholder_dialogue(data):
        raise ValueError("형식 예시(샘플) JSON입니다. 실제 변환 결과를 확인하세요.")
    inputs = data.get("inputs")
    if not isinstance(inputs, list) or not inputs:
        raise ValueError('"inputs" 배열이 비어 있거나 없습니다.')
    for i, item in enumerate(inputs):
        if not isinstance(item, dict):
            raise ValueError(f"inputs[{i}] 는 객체여야 합니다.")
        sp = item.get("speaker")
        tx = item.get("text")
        if not isinstance(sp, str) or not sp.strip():
            raise ValueError(f"inputs[{i}].speaker 가 비어 있습니다.")
        if not isinstance(tx, str) or not tx.strip():
            raise ValueError(f"inputs[{i}].text 가 비어 있습니다.")
    return data


# 흔한 AI 로마자 오표기 → voices 키
_SPEAKER_ALIASES: dict[str, str] = {
    "chung": "cheongheo",
    "cheong": "cheongheo",
    "cheongheo": "cheongheo",
    "cheongheojinin": "cheongheo",
    "qingxu": "cheongheo",
    "seo": "elder",
    "seojawon": "elder",
    "seowon": "elder",
    "xuzhiyuan": "elder",
    "sahyung": "mob",
    "sahyeong": "mob",
    "sahiung": "mob",
    "hyung": "mob",
    "senior": "mob",
    "disciple": "mob",
    "teacher": "elder",
    "master": "cheongheo",
    "hoesaek": "noin",
    "umjungsan": "eom",
    "eomjungsan": "eom",
    "eomjungshan": "eom",
    "umjungshan": "eom",
    "elder_board": "noin",
    "panjuin": "noin",
    "whiteelder": "noin",
    "imsochon": "sochon",
    "limsochon": "sochon",
    "imsocheon": "sochon",
    "namgunglin": "namgung",
    "namgungrin": "namgung",
    "namgungho": "namgungho",
    "namgungjun": "namgungjun",
    "yurim": "yurim",
    "yakjae": "yakjae",
    "insudang": "yakjae",
    "herbalist": "yakjae",
    "ashjin": "ashjin",
    "jaetbit": "ashjin",
    "grayjin": "ashjin",
    "jeokwi": "jeokwi",
    "redguard": "jeokwi",
    "dangga": "dangga",
    "tangclan": "dangga",
    "mokyeon": "mokyeon",
    "mokyun": "mokyeon",
    "mogyeon": "mokyeon",
    "hyeonmu": "hyeonmu",
    "hyunmu": "hyeonmu",
    "hyeonmujin": "hyeonmu",
    "hyunmujin": "hyeonmu",
    "hwan": "hyeonmu",
    "hwanjinin": "hyeonmu",
    "mukheo": "mukheo",
    "meokheo": "mukheo",
    "mukhe": "mukheo",
}

_TAG_START_RE = re.compile(r"^\s*\[[A-Za-z][A-Za-z0-9_\-]*\]")

# 나레이션 문맥·대사 자기지칭 → 마교 백발 노인(noin)
_NOIN_NARR_RE = re.compile(
    r"(노인|백발|노부|판\s*위|사원|판의\s*주인|심어진\s*검|검은\s*옥)"
)
_NOIN_LINE_RE = re.compile(r"(이\s*노부|노부의|노부\b)")
_NOIN_KEEP_SPEAKERS = frozenset({"narrator", "mob", "hoesaek", "noin"})


def _load_voice_key_set(voices_path: Path | str | None) -> set[str]:
    if voices_path is None:
        return set()
    p = Path(voices_path)
    if not p.is_file():
        return set()
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError, ValueError):
        return set()
    if not isinstance(data, dict):
        return set()
    inner = data.get("voices") if isinstance(data.get("voices"), dict) else data
    if not isinstance(inner, dict):
        return set()
    keys: set[str] = set()
    for k, v in inner.items():
        if not isinstance(k, str) or not k.strip():
            continue
        if k.strip().casefold() in {"mob_pool", "voices"}:
            continue
        if isinstance(v, (list, dict)):
            continue
        keys.add(k.strip())
    keys.add("mob")
    return keys


def _map_speaker(raw: str, allowed: set[str]) -> str:
    sp = (raw or "").strip()
    if not sp:
        return "narrator"
    low = sp.casefold()
    allowed_cf = {k.casefold(): k for k in allowed} if allowed else {}
    alias = _SPEAKER_ALIASES.get(low)
    # hoesaek→noin 등: 원키가 허용돼 있어도 별칭 우선 (대상 키가 있을 때만)
    if alias and alias.casefold() != low and alias.casefold() in allowed_cf:
        return allowed_cf[alias.casefold()]
    if low in allowed_cf:
        return allowed_cf[low]
    if alias:
        if alias.casefold() in allowed_cf:
            return allowed_cf[alias.casefold()]
        if alias == "mob" or not allowed:
            return alias
        return "mob"
    if allowed and low not in allowed_cf:
        return "mob"
    return sp


def _resolve_noin_key(allowed: set[str]) -> str | None:
    for k in allowed:
        if k.casefold() == "noin":
            return k
    return None


def _remap_noin_by_context(
    inputs: list[dict[str, Any]],
    *,
    allowed: set[str],
) -> list[dict[str, Any]]:
    """나레이션·대사 문맥으로 mob/hoesaek 노인 대사를 noin 에 가깝게 재매핑."""
    noin = _resolve_noin_key(allowed)
    if noin is None:
        return inputs

    near_noin = False
    out: list[dict[str, Any]] = []
    for item in inputs:
        sp = str(item.get("speaker") or "").strip()
        tx = str(item.get("text") or "")
        low = sp.casefold()
        row = dict(item)

        if low == "narrator":
            if _NOIN_NARR_RE.search(tx):
                near_noin = True
            out.append(row)
            continue

        if low in {"hoesaek", "noin"}:
            row["speaker"] = noin
            near_noin = True
            out.append(row)
            continue

        if low == "mob":
            if near_noin or _NOIN_LINE_RE.search(tx):
                row["speaker"] = noin
                near_noin = True
            out.append(row)
            continue

        if low not in _NOIN_KEEP_SPEAKERS:
            near_noin = False
        out.append(row)
    return out


def _ensure_emotion_tag(text: str) -> str:
    """비나레이션 대사에 [tag] 가 없으면 [calm] 부여, 따옴표 보정."""
    s = (text or "").strip()
    if not s:
        return s
    if _TAG_START_RE.match(s):
        # 태그는 있는데 따옴표가 없으면 본문만 감싸기
        m = _TAG_START_RE.match(s)
        assert m is not None
        rest = s[m.end() :].strip()
        if rest and not (rest.startswith('"') or rest.startswith("“")):
            return f"{m.group(0).strip()} \"{rest.strip('\"“”')}\""
        return s
    body = s
    if (body.startswith('"') and body.endswith('"')) or (
        body.startswith("“") and body.endswith("”")
    ):
        inner = body[1:-1]
        return f'[calm] "{inner}"'
    return f'[calm] "{body}"'


def normalize_dialogue_data(
    data: dict[str, Any],
    *,
    voices_path: Path | str | None = None,
) -> dict[str, Any]:
    """장 제목 inputs[0] 보장·한자 제거·speaker 교정·감정 태그·UI 노이즈 제거."""
    data = strip_ui_noise_from_dialogue(data)
    data = ensure_chapter_title_input(data)
    allowed = _load_voice_key_set(voices_path)
    inputs = data.get("inputs")
    if not isinstance(inputs, list):
        return data
    out_inputs: list[dict[str, Any]] = []
    for item in inputs:
        if not isinstance(item, dict):
            continue
        sp = _map_speaker(str(item.get("speaker") or ""), allowed)
        tx = strip_hanja(
            strip_genspark_ui_noise(str(item.get("text") or "").strip())
        )
        if not tx:
            continue
        if sp.casefold() != "narrator" and tx:
            tx = _ensure_emotion_tag(tx)
        row = dict(item)
        row["speaker"] = sp
        row["text"] = tx
        out_inputs.append(row)
    out_inputs = _remap_noin_by_context(out_inputs, allowed=allowed)
    result = dict(data)
    ch = strip_hanja(str(result.get("chapter") or "").strip())
    if ch:
        result["chapter"] = ch
    result["inputs"] = out_inputs
    result = ensure_chapter_title_input(result)
    if not str(result.get("voices_file") or "").strip():
        result["voices_file"] = "../../../voices.json"
    return result


def normalize_dialogue_json(
    raw_or_data: str | dict[str, Any],
    *,
    voices_path: Path | str | None = None,
) -> str:
    """파싱·정규화 후 pretty JSON 문자열."""
    if isinstance(raw_or_data, dict):
        data = parse_dialogue_json(json.dumps(raw_or_data, ensure_ascii=False))
    else:
        data = parse_dialogue_json(raw_or_data)
    data = normalize_dialogue_data(data, voices_path=voices_path)
    return format_dialogue_json(data)


def format_dialogue_json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def write_dialogue_json(
    dest: Path | str,
    raw_or_data: str | dict[str, Any],
    *,
    voices_path: Path | str | None = None,
) -> Path:
    """검증·정규화 후 ``dest`` 에 저장."""
    if isinstance(raw_or_data, dict):
        data = parse_dialogue_json(json.dumps(raw_or_data, ensure_ascii=False))
    else:
        data = parse_dialogue_json(raw_or_data)
    data = normalize_dialogue_data(data, voices_path=voices_path)
    path = Path(dest).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(format_dialogue_json(data), encoding="utf-8")
    return path.resolve()


def looks_like_dialogue_json(raw: str) -> bool:
    try:
        parse_dialogue_json(raw)
        return True
    except (ValueError, TypeError):
        return False


def dialogue_input_count(raw: str) -> int:
    try:
        return len(parse_dialogue_json(raw).get("inputs") or [])
    except (ValueError, TypeError):
        return 0


def loose_speaker_count(raw: str) -> int:
    """파싱 전 힌트: 페이지에 보이는 speaker 항목 수."""
    return len(_extract_inputs_loose(raw or ""))


def min_inputs_for_source(source_chars: int) -> int:
    """원본 TXT 길이에 따른 최소 inputs (너무 이른 완료 방지)."""
    if source_chars <= 0:
        return 8
    if source_chars < 1500:
        return 6
    if source_chars < 4000:
        return 12
    if source_chars < 8000:
        return 20
    return 30

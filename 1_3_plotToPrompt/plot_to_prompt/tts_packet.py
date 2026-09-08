# -*- coding: utf-8 -*-
"""기존 BRIEF로 젠스파크 합본·tts 저장."""

from __future__ import annotations

import re
from pathlib import Path

from plot_to_prompt.prompt_pack import load_write_rules

_PREV_TAIL = 1200
_CHARS_MAX = 4000

CHAPTER_START = "<<<CHAPTER_START>>>"
CHAPTER_END = "<<<CHAPTER_END>>>"

_MARKER_RE = re.compile(
    re.escape(CHAPTER_START) + r"\s*(.*?)\s*" + re.escape(CHAPTER_END),
    re.DOTALL,
)


# 합본 「작성 지시」에 넣는 예시 — 실제 대본이 아님
_PLACEHOLDER_BODIES = frozenset(
    {
        "(제목 한 줄)\n(본문)",
        "(제목 한 줄)\r\n(본문)",
        "…",
        "...",
    }
)


def looks_like_genspark_packet(text: str) -> bool:
    """합본(프롬프트) 자체인지 — 감시/저장 대상에서 제외."""
    if not text:
        return False
    if "===== 작성 지시 =====" in text:
        return True
    if "===== WRITE_RULES.md =====" in text and CHAPTER_START in text:
        return True
    return False


def _iter_marker_bodies(text: str) -> list[str]:
    """CHAPTER_START/END 사이 본문들 (플레이스홀더·너무 짧은 것 제외). 등장 순."""
    out: list[str] = []
    for m in _MARKER_RE.finditer(text or ""):
        body = m.group(1).replace("\r\n", "\n").strip()
        if not body or body in _PLACEHOLDER_BODIES:
            continue
        # 합본 지시의 한 줄 예시·잘린 조각 제외
        if len(body) < 200:
            continue
        # 예시 제목 자리표시
        if body.startswith("제") and "(제목)" in body[:40]:
            continue
        out.append(body)
    return out


def resolve_clipboard_body(
    text: str,
    *,
    ignore_bodies: set[str] | frozenset[str] | None = None,
) -> tuple[str, str | None]:
    """클립보드 분류. (kind, body) — kind: empty|packet|placeholder|no_markers|body.

    여러 CHAPTER 블록이 있으면 **마지막(최신 응답)** 을 고른다.
    ``ignore_bodies`` 에 있는 이전 장 본문은 제외한다.
    """
    if not (text or "").strip():
        return "empty", None
    bodies = _iter_marker_bodies(text)
    if ignore_bodies:
        bodies = [b for b in bodies if b not in ignore_bodies]
    if bodies:
        return "body", bodies[-1]
    if looks_like_genspark_packet(text):
        return "packet", None
    m = _MARKER_RE.search(text)
    if not m:
        return "no_markers", None
    body = m.group(1).replace("\r\n", "\n").strip()
    if not body or body in _PLACEHOLDER_BODIES or len(body) < 200:
        return "placeholder", None
    return "body", body


def extract_chapter_body(
    text: str,
    *,
    ignore_bodies: set[str] | frozenset[str] | None = None,
) -> str | None:
    """START/END 사이 본문. 없으면 None.

    페이지에 이전 장·합본 예시가 같이 있어도, ignore 제외 후 **최신** 블록을 고른다.
    """
    kind, body = resolve_clipboard_body(text, ignore_bodies=ignore_bodies)
    return body if kind == "body" else None


def strip_outer_code_fence(text: str) -> str:
    """젠스파크가 전체를 ``` 로 감싼 경우 제거."""
    t = (text or "").replace("\r\n", "\n").strip()
    if not t.startswith("```"):
        return t
    lines = t.split("\n")
    if not lines:
        return t
    lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def read_export_text(path: Path | str) -> str:
    """다운로드한 결과 파일 읽기 (utf-8 / cp949 등)."""
    raw = Path(path).expanduser().read_bytes()
    for enc in ("utf-8-sig", "utf-8", "cp949", "euc-kr"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def body_from_export(text: str) -> tuple[str, str]:
    """다운로드 텍스트 → (kind, body). kind: body|raw|packet|placeholder|empty."""
    cleaned = strip_outer_code_fence(text)
    kind, body = resolve_clipboard_body(cleaned)
    if kind == "body" and body:
        return "body", body
    if kind == "packet":
        return "packet", ""
    if kind == "placeholder":
        return "placeholder", ""
    stripped = cleaned.strip()
    if not stripped:
        return "empty", ""
    return "raw", stripped


def resolve_brief_file(novel_root: Path | str, chapter: int) -> Path | None:
    base = Path(novel_root).expanduser() / "briefs"
    if not base.is_dir():
        return None
    for name in (
        f"CHAPTER_{chapter}.md",
        f"CHAPTER_{chapter:02d}.md",
        f"CHAPTER_{chapter:03d}.md",
    ):
        p = base / name
        if p.is_file():
            return p.resolve()
    return None


def ensure_tts(work_root: Path | str) -> Path:
    root = Path(work_root).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    t = root / "tts"
    t.mkdir(parents=True, exist_ok=True)
    return t


def chapter_tts_path(work_root: Path | str, chapter: int) -> Path:
    return Path(work_root).expanduser() / "tts" / f"{int(chapter)}.txt"


_CHAPTER_HEAD_RE = re.compile(
    r"^제\s*0*(\d+)\s*장\s*[-–—:：·.\s]+\s*(.+)$"
)


def format_tts_heading(chapter: int, title: str) -> str:
    """``제23장-환(還)의 얼굴`` 형식."""
    t = (title or "").strip()
    t = re.sub(r"^제\s*0*\d+\s*장\s*[-–—:：·.\s]*", "", t).strip()
    if not t:
        return f"제{int(chapter)}장"
    return f"제{int(chapter)}장-{t}"


def ensure_tts_body_heading(
    chapter: int,
    text: str,
    *,
    title: str = "",
) -> str:
    """본문 맨 위를 ``제N장-제목`` 한 줄로 맞춘다."""
    ch = int(chapter)
    body = (text or "").replace("\r\n", "\n").strip()
    lines = body.split("\n") if body else []
    first = lines[0].strip() if lines else ""

    resolved = (title or "").strip()
    m = _CHAPTER_HEAD_RE.match(first)
    if m:
        if not resolved:
            resolved = m.group(2).strip()
        rest = "\n".join(lines[1:]).lstrip("\n")
    elif first:
        plain = re.sub(r"^제\s*0*\d+\s*장\s*[-–—:：·.\s]*", "", first).strip()
        if not resolved:
            resolved = plain or first
            rest = "\n".join(lines[1:]).lstrip("\n")
        else:
            title_plain = re.sub(
                r"^제\s*0*\d+\s*장\s*[-–—:：·.\s]*", "", resolved
            ).strip()
            if plain == title_plain or first == title_plain or first == resolved:
                rest = "\n".join(lines[1:]).lstrip("\n")
            else:
                rest = body
    else:
        rest = ""

    head = format_tts_heading(ch, resolved)
    if rest:
        out = f"{head}\n\n{rest}".rstrip() + "\n"
    else:
        out = head + "\n"
    return out


def prev_chapter_tail(work_root: Path | str, chapter: int, *, max_chars: int = _PREV_TAIL) -> str:
    if chapter <= 1:
        return ""
    prev = chapter_tts_path(work_root, chapter - 1)
    if not prev.is_file():
        return ""
    try:
        text = prev.read_text(encoding="utf-8-sig").strip()
    except OSError:
        return ""
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]


def build_packet_from_brief(
    *,
    novel_root: Path | str,
    work_root: Path | str,
    chapter: int,
) -> tuple[str, list[str]]:
    """디스크 BRIEF(+ WRITE_RULES·characters·직전 tts 꼬리)로 합본."""
    notes: list[str] = []
    parts: list[str] = []
    novel = Path(novel_root).expanduser()

    wr = novel / "WRITE_RULES.md"
    rules = load_write_rules(wr if wr.is_file() else None)
    parts.append("===== WRITE_RULES.md =====\n" + rules)
    if not wr.is_file():
        notes.append("WRITE_RULES.md 없음 — 내장 규칙 사용")

    bp = resolve_brief_file(novel, chapter)
    if bp is None:
        notes.append(f"briefs/CHAPTER_{chapter}.md 없음")
    else:
        parts.append(f"===== {bp.name} =====\n" + bp.read_text(encoding="utf-8-sig").strip())

    chars = novel / "characters.md"
    if chars.is_file():
        body = chars.read_text(encoding="utf-8-sig").strip()
        if len(body) > _CHARS_MAX:
            body = body[:_CHARS_MAX] + "\n…(이하 생략 — 필요 인물만 추가)…"
            notes.append("characters.md 앞 4000자만")
        parts.append("===== characters.md (발췌) =====\n" + body)
    else:
        notes.append("characters.md 없음")

    tail = prev_chapter_tail(work_root, chapter)
    if tail:
        parts.append(f"===== 직전 장 꼬리 ({chapter - 1}.txt) =====\n" + tail)
    else:
        notes.append(f"직전 tts/{chapter - 1}.txt 없음 — 꼬리 생략")

    parts.append(
        "===== 작성 지시 =====\n"
        f"{chapter}장 본문만 작성하라. WRITE_RULES와 BRIEF 비트를 따른다.\n"
        f"첫 줄은 반드시 ``제{chapter}장-본문제목`` 형식 (예: 제{chapter}장-환(還)의 얼굴).\n"
        "그 다음 빈 줄 후 본문만. 메타·비트 번호·설명 금지.\n"
        "목표 분량 9,000~11,000자.\n\n"
        "【필수】 출력 전체를 아래 구분자로 감싼다. 구분자 밖에는 아무 글도 쓰지 말 것.\n"
        f"{CHAPTER_START}\n"
        f"제{chapter}장-(제목)\n"
        "(본문)\n"
        f"{CHAPTER_END}"
    )
    return "\n\n".join(parts).strip() + "\n", notes


def save_chapter_to_tts(
    work_root: Path | str,
    chapter: int,
    text: str,
    *,
    title: str = "",
    novel_root: Path | str | None = None,
) -> Path:
    """``tts/{장}.txt`` 저장. 맨 위 줄을 ``제N장-제목``으로 맞춘다."""
    ensure_tts(work_root)
    path = chapter_tts_path(work_root, chapter)
    resolved_title = (title or "").strip()
    if not resolved_title and novel_root:
        try:
            from plot_to_prompt.bible_lookup import load_chapter_meta

            resolved_title = load_chapter_meta(novel_root, chapter).body_title
        except Exception:
            resolved_title = ""
    body = ensure_tts_body_heading(chapter, text, title=resolved_title)
    path.write_text(body, encoding="utf-8")
    return path.resolve()

# -*- coding: utf-8 -*-
"""Genspark AI Image 한도(5시간) 배너·토스트 감지."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any


class AiImageLimitError(RuntimeError):
    """AI Image 사용 한도. ``reset_at`` 이 있으면 그 시각까지 대기 권장."""

    def __init__(
        self,
        message: str,
        *,
        reset_at: datetime | None = None,
        raw: str = "",
    ) -> None:
        super().__init__(message)
        self.reset_at = reset_at
        self.raw = (raw or "")[:2000]


BROWSER_CLOSED_MSG = (
    "브라우저 탭이 닫혔습니다. 「실행」를 다시 눌러 주세요."
)


class BrowserClosedError(RuntimeError):
    """Chrome/CDP 브라우저 또는 작업 탭이 닫혀 생성을 계속할 수 없음."""


def is_browser_closed_error(err: BaseException) -> bool:
    if isinstance(err, BrowserClosedError):
        return True
    msg = str(err).lower()
    if BROWSER_CLOSED_MSG in str(err):
        return True
    hints = (
        "target page, context or browser has been closed",
        "browser has been closed",
        "connection closed",
        "browser context",
        "cdp",
    )
    return any(h in msg for h in hints)


@dataclass(frozen=True)
class LimitHit:
    message: str
    reset_at: datetime | None
    snippet: str


_LIMIT_HINT_RE = re.compile(
    r"AI\s*Image\s*.{0,40}제한|"
    r"5\s*시간\s*제한|"
    r"제한에\s*도달|"
    r"재설정됩니다|"
    r"usage\s*limit|"
    r"rate\s*limit|"
    r"fair[\s-]*use|"
    r"try\s*again\s*later|"
    r"quota\s*(?:exceeded|limit)|"
    r"5[\s-]*hour",
    re.IGNORECASE,
)

# 「5시간 제한에 근접했습니다」— 경고만, 생성 계속 가능 → 한도 대기·브라우저 종료 대상 아님
_NEAR_LIMIT_RE = re.compile(
    r"제한에\s*근접|"
    r"근접했습니다|"
    r"approaching\s+(?:the\s+)?(?:\d+[\s-]*hour\s+)?(?:limit|quota)|"
    r"near(?:ing)?\s+(?:the\s+)?(?:limit|quota)",
    re.IGNORECASE,
)

# 근접과 함께 있어도 실제 한도로 볼 문구
_HARD_LIMIT_RE = re.compile(
    r"제한에\s*도달|"
    r"재설정됩니다|"
    r"quota\s*(?:exceeded|limit)|"
    r"usage\s*limit|"
    r"rate\s*limit|"
    r"try\s*again\s*later",
    re.IGNORECASE,
)

# 예: 8월 26일 오전 2:56에 재설정됩니다 / 8월 26일 오후 11:05
_RESET_KO_RE = re.compile(
    r"(?P<mon>\d{1,2})\s*월\s*(?P<day>\d{1,2})\s*일\s*"
    r"(?P<ampm>오전|오후)?\s*"
    r"(?P<hour>\d{1,2})\s*:\s*(?P<minute>\d{2})",
)

# 예: resets on Aug 26 at 2:56 AM / 2026-08-26 02:56
_RESET_EN_RE = re.compile(
    r"(?:reset|resets|available)\s*(?:on|at|:)?\s*"
    r"(?P<mon>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+"
    r"(?P<day>\d{1,2})(?:st|nd|rd|th)?(?:,?\s*(?P<year>\d{4}))?\s*"
    r"(?:at\s*)?(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*(?P<ampm>AM|PM)?",
    re.IGNORECASE,
)
_RESET_ISO_RE = re.compile(
    r"(?P<year>\d{4})-(?P<mon>\d{1,2})-(?P<day>\d{1,2})[ T]"
    r"(?P<hour>\d{1,2}):(?P<minute>\d{2})"
)

_MONTH_EN = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


def text_is_near_limit_only(text: str) -> bool:
    """근접 경고만 있고 실제 도달·재설정 안내가 없으면 True."""
    s = text or ""
    if not _NEAR_LIMIT_RE.search(s):
        return False
    return not bool(_HARD_LIMIT_RE.search(s))


def text_looks_like_limit(text: str) -> bool:
    s = text or ""
    if text_is_near_limit_only(s):
        return False
    return bool(_LIMIT_HINT_RE.search(s))


def parse_reset_at(text: str, *, now: datetime | None = None) -> datetime | None:
    """본문에서 재설정 시각을 파싱. 없으면 None."""
    now = now or datetime.now()
    s = text or ""

    m = _RESET_KO_RE.search(s)
    if m:
        mon = int(m.group("mon"))
        day = int(m.group("day"))
        hour = int(m.group("hour"))
        minute = int(m.group("minute"))
        ampm = m.group("ampm")
        if ampm == "오후" and hour < 12:
            hour += 12
        elif ampm == "오전" and hour == 12:
            hour = 0
        year = now.year
        try:
            dt = datetime(year, mon, day, hour, minute)
        except ValueError:
            return None
        # 이미 지난 시각이면(예: 자정 전후) 다음 해 또는 +12h 보정
        if dt < now - timedelta(minutes=5):
            if (now - dt) > timedelta(hours=18):
                try:
                    dt = datetime(year + 1, mon, day, hour, minute)
                except ValueError:
                    return None
            elif dt.date() == now.date() and dt < now:
                return None
        return dt

    m = _RESET_EN_RE.search(s)
    if m:
        mon = _MONTH_EN.get((m.group("mon") or "")[:3].lower())
        if not mon:
            return None
        day = int(m.group("day"))
        hour = int(m.group("hour"))
        minute = int(m.group("minute"))
        year = int(m.group("year") or now.year)
        ampm = (m.group("ampm") or "").upper()
        if ampm == "PM" and hour < 12:
            hour += 12
        elif ampm == "AM" and hour == 12:
            hour = 0
        try:
            dt = datetime(year, mon, day, hour, minute)
        except ValueError:
            return None
        if dt < now - timedelta(minutes=5) and not m.group("year"):
            try:
                dt = datetime(year + 1, mon, day, hour, minute)
            except ValueError:
                return None
        return dt

    m = _RESET_ISO_RE.search(s)
    if m:
        try:
            return datetime(
                int(m.group("year")),
                int(m.group("mon")),
                int(m.group("day")),
                int(m.group("hour")),
                int(m.group("minute")),
            )
        except ValueError:
            return None
    return None


def limit_hit_from_text(text: str, *, now: datetime | None = None) -> LimitHit | None:
    if not text_looks_like_limit(text):
        return None
    snippet = (text or "").replace("\r", " ")
    # 한도 문구 주변만 남김
    m = _LIMIT_HINT_RE.search(snippet)
    if m:
        i = max(0, m.start() - 40)
        j = min(len(snippet), m.end() + 120)
        ctx = snippet[i:j]
        # 매치 구간에 「근접」만 있으면 한도 아님 (배너: 5시간 제한에 근접했습니다)
        if _NEAR_LIMIT_RE.search(ctx) and not _HARD_LIMIT_RE.search(ctx):
            return None
        snippet = re.sub(r"\s+", " ", ctx).strip()
    else:
        snippet = re.sub(r"\s+", " ", snippet[-400:]).strip()
    reset_at = parse_reset_at(text, now=now)
    msg = "AI Image 한도 감지"
    if reset_at:
        msg = f"AI Image 한도 - 재설정 {reset_at.strftime('%Y-%m-%d %H:%M')}"
    return LimitHit(message=msg, reset_at=reset_at, snippet=snippet[:400])


async def read_page_visible_text(page: Any, *, max_chars: int = 12000) -> str:
    try:
        raw = await page.evaluate(
            """(n) => {
              const body = document.body;
              if (!body) return '';
              // 토스트·배너 우선
              const bits = [];
              const sel = [
                '[role="alert"]', '[role="status"]', '[class*="toast" i]',
                '[class*="snackbar" i]', '[class*="banner" i]',
                '[class*="notification" i]', '[class*="limit" i]'
              ].join(',');
              for (const el of document.querySelectorAll(sel)) {
                const t = (el.innerText || el.textContent || '').trim();
                if (t) bits.push(t);
              }
              const full = (body.innerText || '').trim();
              bits.push(full.slice(Math.max(0, full.length - Math.max(2000, n))));
              return bits.join('\\n---\\n').slice(0, n);
            }""",
            int(max_chars),
        )
        return str(raw or "")
    except Exception:
        return ""


async def detect_limit_on_page(page: Any) -> LimitHit | None:
    text = await read_page_visible_text(page)
    return limit_hit_from_text(text)


def raise_limit_error(hit: LimitHit) -> None:
    raise AiImageLimitError(
        hit.message,
        reset_at=hit.reset_at,
        raw=hit.snippet,
    )


_GENSPARK_LIMIT_HOURS = 5

_SESSION_START_RE = re.compile(
    r"(?P<hour>\d{1,2})\s*[:시h]\s*(?P<minute>\d{1,2})?\s*(?:분)?",
)


def parse_session_start_hm(text: str) -> tuple[int, int] | None:
    """실행 시작 시각 ``14:30`` / ``14시 30분``."""
    s = (text or "").strip()
    if not s:
        return None
    m = _SESSION_START_RE.fullmatch(s) or _SESSION_START_RE.search(s)
    if not m:
        return None
    hour = int(m.group("hour"))
    minute = int(m.group("minute") or 0)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def reset_at_from_session_start(
    hour: int,
    minute: int,
    *,
    now: datetime | None = None,
) -> datetime:
    """실행 시작 + 5시간 = 정상화 예상. 이미 지났으면 5시간씩 앞으로."""
    now = now or datetime.now()
    try:
        start = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    except ValueError:
        return now + timedelta(hours=_GENSPARK_LIMIT_HOURS)
    reset = start + timedelta(hours=_GENSPARK_LIMIT_HOURS)
    while reset <= now + timedelta(seconds=30):
        reset += timedelta(hours=_GENSPARK_LIMIT_HOURS)
    return reset


def resolve_limit_reset_at(
    err: BaseException | str,
    *,
    session_start_hm: tuple[int, int] | None = None,
    now: datetime | None = None,
) -> datetime | None:
    """배너 재설정 시각 → 없으면 실행 시작+5시간."""
    now = now or datetime.now()
    reset_at: datetime | None = None
    raw = ""
    if isinstance(err, AiImageLimitError):
        reset_at = err.reset_at
        raw = err.raw or ""
    text = raw + "\n" + (str(err) if not isinstance(err, str) else err)
    if reset_at is None:
        reset_at = parse_reset_at(text, now=now)
    if reset_at is None and session_start_hm:
        reset_at = reset_at_from_session_start(
            session_start_hm[0], session_start_hm[1], now=now
        )
    return reset_at


def format_reset_at(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    return dt.strftime("%Y-%m-%d %H:%M")

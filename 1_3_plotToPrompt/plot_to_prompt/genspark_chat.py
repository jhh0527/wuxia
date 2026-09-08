# -*- coding: utf-8 -*-
"""Genspark AI Chat — 합본 붙여넣기·장 대본 스크랩 (Playwright CDP)."""

from __future__ import annotations

import asyncio
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from plot_to_prompt import diag_log
from plot_to_prompt.paths import GENSPARK_AI_CHAT_URL
from plot_to_prompt.tts_packet import (
    CHAPTER_END,
    CHAPTER_START,
    _iter_marker_bodies,
    extract_chapter_body,
    looks_like_genspark_packet,
)

# 모듈 전용 ChromeDebug (1_5=9222, 2_4=9232, 2_5=9242 와 분리 — 동시 실행 가능)
_CDP_PORT = 9213
_CDP_PORTS = (9213,)
_CHROME_DEBUG_USER_DATA = Path(r"C:\ChromeDebug_1_3")
_PROFILE_DIRNAME = ".genspark_plot_to_prompt_profile"
# Genspark AI Chat 모델 (브라우저 열기·변환 시 자동 선택)
_TARGET_CHAT_MODEL = "Claude Opus 4.6"
_TARGET_CHAT_MODEL_RE = re.compile(
    r"claude\s*opus\s*4\s*[.\s]?6|opus\s*4\s*[.\s]?6",
    re.IGNORECASE,
)


def find_chrome_exe() -> Path | None:
    candidates: list[Path] = [
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    ]
    for key in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        base = os.environ.get(key, "")
        if base:
            candidates.append(
                Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe"
            )
    seen: set[str] = set()
    for p in candidates:
        key = str(p).lower()
        if key in seen:
            continue
        seen.add(key)
        if p.is_file():
            return p
    return None


def close_chrome_debug(*, user_data_dir: Path | None = None) -> None:
    """이 모듈 ChromeDebug(CDP)만 종료 — 다른 모듈 포트·프로필은 건드리지 않음."""
    reset_session()
    data = str(Path(user_data_dir or _CHROME_DEBUG_USER_DATA).resolve())
    data_esc = data.replace("'", "''")
    if sys.platform == "win32":
        # user-data-dir 일치, 또는 이 모듈 전용 포트(+해당 프로필)만 종료
        ps = (
            "$ud='"
            + data_esc
            + "';"
            "$ports=@("
            + ",".join(str(p) for p in _CDP_PORTS)
            + ");"
            "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
            "ForEach-Object {"
            "  $cl=$_.CommandLine; if(-not $cl){return};"
            "  $hit=$false;"
            "  if($cl -like ('*'+$ud+'*')){$hit=$true};"
            "  foreach($p in $ports){"
            "    if(($cl -like ('*--remote-debugging-port='+$p+'*')) -and "
            "       ($cl -like ('*'+$ud+'*'))){$hit=$true}"
            "  };"
            "  if($hit){Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue}"
            "}"
        )
        try:
            subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    ps,
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    time.sleep(0.8)


def clear_chrome_session_restore(user_data_dir: Path | None = None) -> None:
    """이전 창·탭 복원을 막아 탭이 쌓이지 않게 한다."""
    root = Path(user_data_dir or _CHROME_DEBUG_USER_DATA)
    default = root / "Default"
    if not default.is_dir():
        return
    for name in (
        "Current Session",
        "Current Tabs",
        "Last Session",
        "Last Tabs",
        "Session Storage",
    ):
        p = default / name
        try:
            if p.is_file():
                p.unlink()
            elif p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
        except OSError:
            pass
    sessions = default / "Sessions"
    if sessions.is_dir():
        shutil.rmtree(sessions, ignore_errors=True)


def wait_cdp_ready(*, debug_port: int = _CDP_PORT, timeout_sec: float = 45.0) -> bool:
    """ChromeDebug remote debugging 포트가 응답할 때까지 대기."""
    import urllib.error
    import urllib.request

    url = f"http://127.0.0.1:{int(debug_port)}/json/version"
    deadline = time.time() + max(5.0, float(timeout_sec))
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2.0) as resp:
                if getattr(resp, "status", 200) == 200:
                    return True
        except (urllib.error.URLError, TimeoutError, OSError):
            pass
        time.sleep(0.4)
    return False


def has_playwright() -> bool:
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False
    return True


async def _is_logged_in(page: Any) -> bool:
    """이미 로그인된 세션인지 휴리스틱 판별."""
    try:
        url = (page.url or "").lower()
        if "accounts.google.com" in url:
            return False
        return bool(
            await page.evaluate(
                """() => {
                  const t = ((document.body && document.body.innerText) || '').slice(0, 12000);
                  if (/accounts\\.google\\.com/i.test(location.href)) return false;
                  // 로그인/회원가입 모달 (프로필 초기화 직후 흔함)
                  if (/로그인\\s*또는\\s*회원가입|회원가입을\\s*시작|Genspark AI Workspace를\\s*잠금/i.test(t))
                    return false;
                  if (/Google로\\s*계속하기|Apple로\\s*계속하기|Continue with Apple/i.test(t)
                      && /로그인|Sign\\s*in|Log\\s*in|회원가입/i.test(t))
                    return false;
                  const needsLogin = /Sign\\s*in|Log\\s*in|로그인|Continue with Google|Google로\\s*계속/i.test(t)
                    && !/Sign\\s*out|Log\\s*out|로그아웃/i.test(t);
                  if (needsLogin) return false;
                  const hasAvatar = !!document.querySelector(
                    'img[alt*="avatar" i], img[alt*="profile" i], [data-testid*="avatar" i]'
                  );
                  const hasUserMenu = !!document.querySelector(
                    '[aria-label*="account" i], [aria-label*="Account" i], [aria-label*="프로필" i]'
                  );
                  if (hasAvatar || hasUserMenu) return true;
                  const hasPrompt = !!document.querySelector(
                    "textarea, [contenteditable='true'], [role='textbox']"
                  );
                  return hasPrompt && !needsLogin;
                }"""
            )
        )
    except Exception:
        return False


async def _login_wall_visible(page: Any) -> bool:
    """「로그인 또는 회원가입」 모달 등 로그인 벽이 보이는지."""
    try:
        return bool(
            await page.evaluate(
                """() => {
                  const t = ((document.body && document.body.innerText) || '').slice(0, 12000);
                  return /로그인\\s*또는\\s*회원가입|Google로\\s*계속하기|Continue with Google/i.test(t)
                    && /로그인|Sign\\s*in|Log\\s*in|회원가입|Workspace/i.test(t);
                }"""
            )
        )
    except Exception:
        return False


async def _google_account_chooser_visible(page: Any) -> bool:
    """Google 계정 선택·비밀번호 팝업이 채팅을 가리는지."""
    try:
        url = (page.url or "").lower()
        if "accounts.google.com" in url:
            return True
        return bool(
            await page.evaluate(
                """() => {
                  const t = ((document.body && document.body.innerText) || '').slice(0, 8000);
                  if (/이메일을\\s*잊으셨나요|비밀번호를\\s*입력|Forgot\\s*email|Choose\\s*an\\s*account/i.test(t))
                    return true;
                  if (/@gmail\\.com|accounts\\.google/i.test(t)
                      && document.querySelector('input[type="email"], #identifierId, input[name="Passwd"], input[type="password"]'))
                    return true;
                  if (document.querySelector('[class*="vfppkd" i]')
                      && /gmail|google|이메일|비밀번호/i.test(t))
                    return true;
                  return false;
                }"""
            )
        )
    except Exception:
        return False


async def _clear_google_blocker(
    page: Any,
    *,
    email: str,
    password: str,
    context: Any | None = None,
) -> bool:
    """전송 전 Google 팝업이 있으면 로그인/계정 선택으로 치운다."""
    if not await _google_account_chooser_visible(page) and not await _login_wall_visible(
        page
    ):
        return True
    diag_log.log("Google 로그인/계정 팝업 감지 — 자동 처리 시도")
    if (email or "").strip() and password:
        info = await _ensure_login(
            page, email, password, context=context, force=True
        )
        if info.get("logged_in") and not await _google_account_chooser_visible(page):
            diag_log.log("Google 팝업 처리 완료")
            return True
    # 계정 칩만 클릭 가능한 경우
    if (email or "").strip():
        if await _pick_google_account(page, email):
            await page.wait_for_timeout(1200)
            if password:
                await _fill_google_credentials(page, email, password)
            await page.wait_for_timeout(800)
    still = await _google_account_chooser_visible(page) or await _login_wall_visible(
        page
    )
    if still:
        diag_log.log("Google 팝업이 남아 있음 — 수동 로그인 필요할 수 있음")
        return False
    return True


async def _type_into(page: Any, selectors: tuple[str, ...], text: str) -> bool:
    for sel in selectors:
        loc = page.locator(sel).first
        try:
            if not await loc.is_visible(timeout=2500):
                continue
            await loc.click(timeout=4000)
            await page.wait_for_timeout(200)
            try:
                await loc.fill("")
            except Exception:
                pass
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Backspace")
            await page.keyboard.type(text, delay=25)
            await page.wait_for_timeout(300)
            return True
        except Exception:
            continue
    return False


async def _click_by_text(page: Any, texts: tuple[str, ...]) -> bool:
    for text in texts:
        for sel in (
            f"button:has-text('{text}')",
            f"[role='button']:has-text('{text}')",
            f"[role='option']:has-text('{text}')",
            f"[role='menuitem']:has-text('{text}')",
            f"li:has-text('{text}')",
            f"a:has-text('{text}')",
            f"label:has-text('{text}')",
            f"span:has-text('{text}')",
            f"div:has-text('{text}')",
        ):
            loc = page.locator(sel).first
            try:
                if not await loc.is_visible(timeout=800):
                    continue
                await loc.click(timeout=4000, force=False)
                await page.wait_for_timeout(600)
                return True
            except Exception:
                continue
    return False


async def _click_login_entry(page: Any) -> bool:
    """로그인 모달의 Google 버튼만 클릭 (일반「로그인」문구는 오클릭·지연 유발)."""
    try:
        hit = await page.evaluate(
            """() => {
              const want = /Google로\\s*계속하기|Continue with Google|Sign in with Google/i;
              const nodes = Array.from(document.querySelectorAll(
                'button, [role="button"], a, div[role="button"]'
              ));
              for (const el of nodes) {
                const t = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                if (!want.test(t) || t.length > 48) continue;
                const r = el.getBoundingClientRect();
                if (r.width < 80 || r.height < 24) continue;
                el.click();
                return t;
              }
              return '';
            }"""
        )
        if hit:
            await page.wait_for_timeout(400)
            return True
    except Exception:
        pass
    for text in (
        "Google로 계속하기",
        "Continue with Google",
        "Google로 계속",
        "Sign in with Google",
    ):
        if await _click_by_text(page, (text,)):
            return True
    return False


async def _pick_google_account(page: Any, email: str) -> bool:
    email = (email or "").strip()
    if not email:
        return False
    for sel in (
        f'div[data-identifier="{email}"]',
        f'div[data-email="{email}"]',
        f'[data-identifier="{email}"]',
        f'text="{email}"',
        f'div:has-text("{email}")',
        f'li:has-text("{email}")',
        f'div[role="link"]:has-text("{email}")',
    ):
        loc = page.locator(sel).first
        try:
            if await loc.is_visible(timeout=1500):
                await loc.click(timeout=4000)
                await page.wait_for_timeout(1000)
                return True
        except Exception:
            continue
    return False


async def _click_next(page: Any) -> None:
    for text in ("Next", "다음", "Continue", "계속"):
        if await _click_by_text(page, (text,)):
            return
    try:
        await page.keyboard.press("Enter")
    except Exception:
        pass


async def _fill_google_credentials(page: Any, email: str, password: str) -> bool:
    email = (email or "").strip()
    password = password or ""
    if not email or not password:
        return False
    await _pick_google_account(page, email)
    email_ok = await _type_into(
        page,
        (
            'input[type="email"]',
            "#identifierId",
            'input[name="identifier"]',
            'input[autocomplete="username"]',
        ),
        email,
    )
    if email_ok:
        await _click_next(page)
        await page.wait_for_timeout(900)
    await _pick_google_account(page, email)
    await page.wait_for_timeout(300)
    pw_ok = False
    for _ in range(8):
        pw_ok = await _type_into(
            page,
            (
                'input[type="password"]',
                'input[name="Passwd"]',
                'input[name="password"]',
                'input[autocomplete="current-password"]',
            ),
            password,
        )
        if pw_ok:
            break
        if await _type_into(
            page,
            ('input[type="email"]', "#identifierId", 'input[name="identifier"]'),
            email,
        ):
            await _click_next(page)
        await page.wait_for_timeout(500)
    if not pw_ok:
        return False
    await _click_next(page)
    await page.wait_for_timeout(1000)
    for text in (
        "Not now",
        "나중에",
        "Skip",
        "건너뛰기",
        "Continue",
        "계속",
        "Yes",
        "확인",
        "I understand",
        "이해했습니다",
    ):
        try:
            if await _click_by_text(page, (text,)):
                await page.wait_for_timeout(400)
        except Exception:
            pass
    return True


async def _wait_back_to_genspark(page: Any, *, seconds: int = 25) -> bool:
    for _ in range(max(1, seconds * 2)):
        url = (page.url or "").lower()
        if "genspark.ai" in url and "accounts.google.com" not in url:
            await page.wait_for_timeout(400)
            return True
        await page.wait_for_timeout(400)
    return "genspark.ai" in (page.url or "").lower()


async def _google_login(
    page: Any,
    email: str,
    password: str,
    *,
    context: Any | None = None,
) -> bool:
    email = (email or "").strip()
    password = password or ""
    if not email or not password:
        return False
    login_page = page
    popup: Any | None = None
    for attempt in range(2):
        popup = None
        if context is not None:
            try:
                async with context.expect_page(timeout=5000) as pi:
                    clicked = await _click_login_entry(page)
                    if not clicked:
                        diag_log.log("Google 로그인 버튼 미발견")
                popup = await pi.value
            except Exception:
                await _click_login_entry(page)
        else:
            await _click_login_entry(page)
        if popup is not None:
            try:
                await popup.wait_for_load_state("domcontentloaded", timeout=12_000)
            except Exception:
                pass
            login_page = popup
            break
        # 같은 탭 리다이렉트 / 이미 열린 Google 탭
        for _ in range(10):
            if "accounts.google.com" in (page.url or "").lower():
                login_page = page
                break
            if context is not None:
                for p in context.pages:
                    if "accounts.google.com" in (p.url or "").lower():
                        login_page = p
                        break
                else:
                    await page.wait_for_timeout(300)
                    continue
                break
            await page.wait_for_timeout(300)
        else:
            if attempt == 0:
                continue
        break

    for _ in range(15):
        cur = (login_page.url or "").lower()
        try:
            has_input = await login_page.locator(
                'input[type="email"], input[type="password"], #identifierId'
            ).count()
        except Exception:
            has_input = 0
        if "accounts.google.com" in cur or has_input:
            break
        await page.wait_for_timeout(300)
        if context is not None:
            for p in context.pages:
                if "accounts.google.com" in (p.url or "").lower():
                    login_page = p
                    break

    filled = await _fill_google_credentials(login_page, email, password)
    if not filled:
        diag_log.log("Google 자격증명 입력 실패 — 계정 선택·비밀번호 칸 확인")
        return False
    if login_page is not page:
        try:
            await login_page.wait_for_event("close", timeout=15000)
        except Exception:
            pass
        try:
            if not login_page.is_closed():
                await _wait_back_to_genspark(login_page, seconds=15)
        except Exception:
            pass
    ok = await _wait_back_to_genspark(page, seconds=20)
    if ok:
        return True
    return await _is_logged_in(page) and not await _login_wall_visible(page)


async def _ensure_login(
    page: Any,
    email: str,
    password: str,
    *,
    context: Any | None = None,
    force: bool = False,
) -> dict[str, bool]:
    # 쿠키 세션이 늦게 반영되는 경우 짧게 대기
    wall = await _login_wall_visible(page)
    if wall and not force:
        await page.wait_for_timeout(800)
        wall = await _login_wall_visible(page)
        if not wall and await _is_logged_in(page):
            return {"logged_in": True, "attempted": False, "filled": False}
    # 모달이 없고 이미 로그인이면 본문 「로그인」 문구만으로 재시도하지 않음
    if not force and not wall and await _is_logged_in(page):
        return {"logged_in": True, "attempted": False, "filled": False}
    if not force and not wall:
        # 로그인 벽 없음 → 세션 유지로 보고 통과 (오탐 방지)
        return {"logged_in": True, "attempted": False, "filled": False}
    if not (email or "").strip() or not password:
        if wall:
            diag_log.log("로그인 모달 감지 — GUI에 이메일·비밀번호가 없습니다")
        return {"logged_in": False, "attempted": False, "filled": False}
    diag_log.log(f"자동 로그인 시도 wall={wall} force={force} email={email[:3]}…")
    ok = await _google_login(page, email, password, context=context)
    for _ in range(10):
        if not await _login_wall_visible(page) and await _is_logged_in(page):
            break
        await page.wait_for_timeout(400)
    logged = bool(ok or await _is_logged_in(page)) and not await _login_wall_visible(
        page
    )
    if not logged and await _login_wall_visible(page):
        diag_log.log("자동 로그인 후에도 로그인 모달이 남아 있음")
    return {
        "logged_in": logged,
        "attempted": True,
        "filled": True,
    }


def open_chrome_debug(
    url: str = GENSPARK_AI_CHAT_URL,
    *,
    debug_port: int = _CDP_PORT,
    user_data_dir: Path | None = None,
    restart: bool = True,
) -> dict[str, str]:
    """고정 디버그 Chrome 실행.

    ``restart=True`` 이면 기존 ChromeDebug 을 종료한 뒤 다시 연다.
    ``restart=False`` 이고 CDP 가 이미 응답하면 재실행하지 않는다.
    """
    data_dir = Path(user_data_dir or _CHROME_DEBUG_USER_DATA)
    if not restart and wait_cdp_ready(debug_port=debug_port, timeout_sec=1.2):
        return {
            "mode": "chrome_debug",
            "debug_port": str(debug_port),
            "user_data": str(data_dir.resolve()),
            "reused": "1",
        }
    if restart:
        close_chrome_debug(user_data_dir=user_data_dir)
        clear_chrome_session_restore(user_data_dir)
    chrome = find_chrome_exe()
    if chrome is None:
        raise RuntimeError(
            "Google Chrome을 찾을 수 없습니다.\nChrome 설치 후 다시 시도하세요."
        )
    data_dir.mkdir(parents=True, exist_ok=True)
    reset_session()
    args: list[str] = [
        str(chrome),
        f"--remote-debugging-port={int(debug_port)}",
        f"--user-data-dir={data_dir.resolve()}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-session-crashed-bubble",
        "--new-window",
        url,
    ]
    kwargs: dict = {"args": args, "close_fds": True}
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    subprocess.Popen(**kwargs)
    if not wait_cdp_ready(debug_port=debug_port, timeout_sec=45.0):
        raise RuntimeError(
            f"ChromeDebug(CDP :{debug_port})에 연결하지 못했습니다.\n"
            "Chrome이 완전히 뜬 뒤 「실행」를 다시 눌러 주세요."
        )
    return {
        "mode": "chrome_debug",
        "debug_port": str(debug_port),
        "user_data": str(data_dir.resolve()),
        "reused": "0",
    }


async def _launch_context(playwright: Any, profile_dir: Path) -> Any:
    for _ in range(24):
        for port in _CDP_PORTS:
            try:
                browser = await playwright.chromium.connect_over_cdp(
                    f"http://127.0.0.1:{port}"
                )
                if browser.contexts:
                    return browser.contexts[0]
            except Exception:
                continue
        await asyncio.sleep(0.5)

    profile_dir.mkdir(parents=True, exist_ok=True)
    kwargs: dict[str, Any] = {
        "user_data_dir": str(profile_dir.resolve()),
        "channel": "chrome",
        "headless": False,
        "locale": "ko-KR",
        "accept_downloads": True,
        "args": ["--disable-blink-features=AutomationControlled"],
    }
    try:
        return await playwright.chromium.launch_persistent_context(**kwargs)
    except Exception:
        browser = await playwright.chromium.launch(
            channel="chrome",
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        return await browser.new_context(locale="ko-KR", accept_downloads=True)


async def _composer_locator(page: Any) -> Any | None:
    """하단 채팅 입력창 locator."""
    for sel in (
        "textarea:visible",
        "[contenteditable='true']:visible",
        "[role='textbox']:visible",
    ):
        loc = page.locator(sel)
        try:
            n = await loc.count()
        except Exception:
            continue
        for i in range(min(n, 8)):
            item = loc.nth(i)
            try:
                if not await item.is_visible():
                    continue
                tag = await item.evaluate(
                    "el => ((el.tagName||'')+(el.getAttribute('type')||'')).toLowerCase()"
                )
                if "file" in (tag or ""):
                    continue
                box = await item.bounding_box()
                if not box or box.get("width", 0) < 80 or box.get("height", 0) < 18:
                    continue
                return item
            except Exception:
                continue
    return None


async def _composer_text_len(page: Any) -> int:
    try:
        return int(
            await page.evaluate(
                """() => {
                  const els = Array.from(document.querySelectorAll(
                    "textarea, [contenteditable='true'], [role='textbox']"
                  ));
                  let best = 0;
                  for (const el of els) {
                    const r = el.getBoundingClientRect();
                    if (r.width < 40 || r.height < 18) continue;
                    const t = (el.value || el.innerText || el.textContent || '').trim();
                    if (t.length > best) best = t.length;
                  }
                  return best;
                }"""
            )
        )
    except Exception:
        return 0


async def _set_composer_value(page: Any, item: Any, text: str) -> bool:
    """프레임워크 state 까지 반영되도록 입력창에 본문 주입."""
    await item.click(timeout=4000)
    await page.wait_for_timeout(120)
    # 1) Playwright fill — input 이벤트 포함
    try:
        await item.fill(text, timeout=90_000)
        await page.wait_for_timeout(250)
        if await _composer_text_len(page) >= min(400, max(50, len(text) // 4)):
            return True
    except Exception:
        pass
    # 2) execCommand / paste 이벤트 (TipTap·ProseMirror 대응)
    try:
        ok = await item.evaluate(
            """(el, t) => {
              el.focus();
              try { document.execCommand('selectAll', false, null); } catch (e) {}
              try {
                if (document.execCommand('insertText', false, t)) {
                  el.dispatchEvent(new InputEvent('input', {
                    bubbles: true, cancelable: true,
                    inputType: 'insertText', data: t
                  }));
                  return true;
                }
              } catch (e) {}
              try {
                const dt = new DataTransfer();
                dt.setData('text/plain', t);
                el.dispatchEvent(new ClipboardEvent('paste', {
                  bubbles: true, cancelable: true, clipboardData: dt
                }));
                return true;
              } catch (e) {}
              if ('value' in el) {
                el.value = t;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                return (el.value || '').length > 0;
              }
              el.textContent = t;
              el.dispatchEvent(new InputEvent('input', {
                bubbles: true, inputType: 'insertFromPaste', data: t
              }));
              return (el.innerText || '').length > 0;
            }""",
            text,
        )
        await page.wait_for_timeout(300)
        if ok and await _composer_text_len(page) >= min(400, max(50, len(text) // 4)):
            return True
    except Exception:
        pass
    # 3) OS 클립보드 + Ctrl+V
    try:
        await page.evaluate(
            """async (t) => { await navigator.clipboard.writeText(t); }""",
            text,
        )
        await item.click(timeout=2000)
        await page.keyboard.press("Control+A")
        await page.wait_for_timeout(80)
        await page.keyboard.press("Control+V")
        await page.wait_for_timeout(400)
        # React 깨우기
        await page.keyboard.type(" ", delay=20)
        await page.keyboard.press("Backspace")
        await page.wait_for_timeout(200)
        if await _composer_text_len(page) >= min(400, max(50, len(text) // 4)):
            return True
    except Exception:
        pass
    try:
        await item.click(timeout=2000)
        await page.keyboard.press("Control+A")
        await page.keyboard.insert_text(text)
        await page.wait_for_timeout(300)
        return await _composer_text_len(page) >= min(200, max(40, len(text) // 8))
    except Exception:
        return False


async def _fill_first_editable(page: Any, text: str) -> bool:
    """입력창에 본문 넣기 — fill → insertText → 클립보드 순."""
    if not (text or "").strip():
        return False
    item = await _composer_locator(page)
    if item is None:
        return False
    ok = await _set_composer_value(page, item, text)
    diag_log.log(
        f"입력창채움 ok={ok} chars_in={len(text)} "
        f"chars_ui={await _composer_text_len(page)}"
    )
    return ok


async def _mouse_click_xy(page: Any, x: float, y: float) -> bool:
    try:
        await page.mouse.click(x, y)
        return True
    except Exception:
        return False


async def _mouse_click_box(page: Any, box: dict[str, float] | None) -> bool:
    if not box:
        return False
    return await _mouse_click_xy(
        page,
        box["x"] + box["width"] / 2,
        box["y"] + box["height"] / 2,
    )


_SEND_SCAN_JS = """() => {
  const vh = window.innerHeight || 800;
  const vw = window.innerWidth || 1200;
  const all = [];
  const walk = (root) => {
    let nodes;
    try { nodes = root.querySelectorAll('*'); }
    catch (e) { return; }
    for (const el of nodes) {
      all.push(el);
      if (el.shadowRoot) walk(el.shadowRoot);
    }
  };
  walk(document);
  let composer = null;
  for (const el of all) {
    const tag = (el.tagName || '').toLowerCase();
    const ce = el.getAttribute && el.getAttribute('contenteditable');
    const role = el.getAttribute && el.getAttribute('role');
    if (!(tag === 'textarea' || ce === 'true' || role === 'textbox')) continue;
    const r = el.getBoundingClientRect();
    if (r.width > 120 && r.height > 24) {
      if (!composer || r.height >= composer.h)
        composer = { x: r.left, y: r.top, w: r.width, h: r.height };
    }
  }
  const hits = [];
  for (const el of all) {
    const tag = (el.tagName || '').toLowerCase();
    const role = (el.getAttribute && el.getAttribute('role')) || '';
    const hasSvg = !!(el.querySelector && el.querySelector('svg'));
    const clickable = tag === 'button' || role === 'button'
      || tag === 'a'
      || (typeof el.tabIndex === 'number' && el.tabIndex >= 0)
      || hasSvg;
    if (!clickable) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 12 || r.height < 12) continue;
    if (r.width > 180 || r.height > 180) continue;
    if (r.bottom < vh * 0.28 || r.top > vh - 1) continue;
    if (composer) {
      const inRight = r.left >= composer.x + composer.w * 0.45;
      const inBottom = r.bottom >= composer.y + composer.h * 0.40;
      const inBox = r.left >= composer.x - 16
        && r.right <= composer.x + composer.w + 56
        && r.top >= composer.y - 16
        && r.bottom <= composer.y + composer.h + 90;
      if (!(inRight && inBottom && inBox)) continue;
    } else if (r.left < vw * 0.30) {
      continue;
    }
    const t = (
      ((el.getAttribute && el.getAttribute('aria-label')) || '')
      + ' ' + ((el.getAttribute && el.getAttribute('title')) || '')
      + ' ' + ((el.getAttribute && el.getAttribute('data-testid')) || '')
      + ' ' + ((el.className && String(el.className)) || '')
      + ' ' + ((el.innerText) || '')
    ).toLowerCase().replace(/\\s+/g, ' ').trim();
    if (/attach|upload|파일|첨부|mic|microphone|voice|image|사진|model|모델|opus|claude|login|로그인/.test(t))
      continue;
    // Google 계정 선택/로그인 팝업 — 전송 버튼으로 오인 금지
    if (/vfppkd|accounts\\.google|google 서비스|이메일을 잊|약관|개인정보|아래로 스크롤|forgot|privacy|terms/.test(t))
      continue;
    let score = (hasSvg ? 10 : 0)
      + ((tag === 'button' || role === 'button') ? 8 : 0)
      + (/enter-icon|send|전송|submit|ask|보내|arrow|paper/.test(t) ? 30 : 0)
      + (/enter-icon/.test(t) ? 40 : 0)
      + (r.left / vw) * 10;
    hits.push({
      score, x: r.left + r.width / 2, y: r.top + r.height / 2,
      t: t.slice(0, 48), tag: tag.toUpperCase(),
      w: Math.round(r.width), h: Math.round(r.height),
    });
  }
  hits.sort((a, b) => b.score - a.score || b.x - a.x);
  return { n: hits.length, top: hits.slice(0, 8), composer };
}"""


async def _click_send_button(page: Any) -> str:
    """전송 버튼 클릭 — enter-icon 우선, Google 팝업 버튼 제외."""
    for sel in (
        "[class*='enter-icon' i]",
        "div.enter-icon",
        "button:has-text('Send')",
        "button:has-text('전송')",
        "button:has-text('Submit')",
        "button:has-text('실행')",
        "button:has-text('Ask')",
        "button[type='submit']:visible",
        "[aria-label*='Send' i]",
        "[aria-label*='전송' i]",
        "[aria-label*='submit' i]",
        "[aria-label*='Send message' i]",
        "[data-testid*='send' i]",
        "button[class*='send' i]",
        "[class*='send-button' i]",
        "[class*='SendButton' i]",
    ):
        btn = page.locator(sel).first
        try:
            if await btn.is_visible(timeout=350):
                try:
                    await btn.evaluate(
                        """el => {
                          el.removeAttribute('disabled');
                          el.disabled = false;
                          el.setAttribute('aria-disabled', 'false');
                          if (el.classList) el.classList.remove('disabled');
                        }"""
                    )
                except Exception:
                    pass
                box = await btn.bounding_box()
                if await _mouse_click_box(page, box):
                    return f"mouse:{sel}"
                await btn.click(timeout=4000, force=True)
                return f"sel:{sel}"
        except Exception:
            continue

    info: dict[str, Any] = {}
    frames = [page]
    try:
        frames.extend(list(page.frames or []))
    except Exception:
        pass
    seen: set[int] = set()
    for fr in frames:
        try:
            fid = id(fr)
            if fid in seen:
                continue
            seen.add(fid)
            raw = await fr.evaluate(_SEND_SCAN_JS)
            if raw and (raw.get("n") or 0) > 0:
                info = raw
                break
            if raw and raw.get("composer") and not info.get("composer"):
                info = raw
        except Exception:
            continue

    tops = list((info or {}).get("top") or [])
    composer = (info or {}).get("composer")
    diag_log.log(
        f"전송후보 n={(info or {}).get('n', 0)} "
        f"composer={composer} "
        f"top={[(h.get('tag'), h.get('w'), h.get('h'), round(h.get('x', 0)), round(h.get('y', 0)), h.get('t')) for h in tops[:5]]}"
    )
    _bad = (
        "vfppkd",
        "google",
        "이메일",
        "약관",
        "스크롤",
        "forgot",
        "privacy",
        "terms",
        "accounts",
    )
    for h in tops:
        label = str(h.get("t") or "").lower()
        if any(b in label for b in _bad):
            continue
        if await _mouse_click_xy(page, float(h["x"]), float(h["y"])):
            await page.wait_for_timeout(200)
            return f"geo:{h.get('tag')}:{h.get('t') or ''}"

    if composer:
        for dx, dy in ((28, 28), (40, 28), (24, 40), (48, 36), (20, 24), (56, 28)):
            x = float(composer["x"]) + float(composer["w"]) - dx
            y = float(composer["y"]) + float(composer["h"]) - dy
            if await _mouse_click_xy(page, x, y):
                diag_log.log(f"전송좌표클릭 x={round(x)} y={round(y)} pad={dx},{dy}")
                await page.wait_for_timeout(250)
                return f"corner:{dx},{dy}"

    try:
        vh = float(await page.evaluate("() => innerHeight"))
        vw = float(await page.evaluate("() => innerWidth"))
        btns = page.locator("button:visible")
        n = await btns.count()
        diag_log.log(f"visible button n={n}")
        for i in range(min(n, 50)):
            b = btns.nth(i)
            try:
                box = await b.bounding_box()
                if not box:
                    continue
                if box["y"] < vh * 0.30:
                    continue
                if box["x"] < vw * 0.35:
                    continue
                if box["width"] > 160 or box["height"] > 160:
                    continue
                label = (
                    (await b.get_attribute("aria-label") or "")
                    + " "
                    + (await b.inner_text(timeout=400) or "")
                ).lower()
                if any(
                    k in label
                    for k in (
                        "attach",
                        "upload",
                        "model",
                        "opus",
                        "claude",
                        "mic",
                        "google",
                        "이메일",
                        "약관",
                        "스크롤",
                        "vfppkd",
                    )
                ):
                    continue
                if await _mouse_click_box(page, box):
                    return f"btnscan:{i}:{label[:30]}"
            except Exception:
                continue
    except Exception as ex:
        diag_log.log(f"btnscan실패 {ex}")
    return ""


async def _ctrl_enter(page: Any, item: Any | None) -> str:
    """Control+Enter — CDP 키 이벤트 우선."""
    if item is not None:
        try:
            await item.click(timeout=2000)
        except Exception:
            pass
    try:
        client = await page.context.new_cdp_session(page)
        await client.send(
            "Input.dispatchKeyEvent",
            {
                "type": "keyDown",
                "modifiers": 2,
                "windowsVirtualKeyCode": 17,
                "code": "ControlLeft",
                "key": "Control",
            },
        )
        await client.send(
            "Input.dispatchKeyEvent",
            {
                "type": "keyDown",
                "modifiers": 2,
                "windowsVirtualKeyCode": 13,
                "code": "Enter",
                "key": "Enter",
                "text": "\r",
            },
        )
        await client.send(
            "Input.dispatchKeyEvent",
            {
                "type": "keyUp",
                "modifiers": 2,
                "windowsVirtualKeyCode": 13,
                "code": "Enter",
                "key": "Enter",
            },
        )
        await client.send(
            "Input.dispatchKeyEvent",
            {
                "type": "keyUp",
                "modifiers": 0,
                "windowsVirtualKeyCode": 17,
                "code": "ControlLeft",
                "key": "Control",
            },
        )
        try:
            await client.detach()
        except Exception:
            pass
        return "cdp:Control+Enter"
    except Exception as ex:
        diag_log.log(f"cdp키실패 {ex}")
    try:
        await page.keyboard.down("Control")
        await page.keyboard.press("Enter")
        await page.keyboard.up("Control")
        return "kbd:Control+Enter"
    except Exception:
        try:
            await page.keyboard.up("Control")
        except Exception:
            pass
    try:
        ok = await page.evaluate(
            """() => {
              const els = Array.from(document.querySelectorAll(
                "textarea, [contenteditable='true'], [role='textbox']"
              ));
              let el = null;
              for (const e of els) {
                const r = e.getBoundingClientRect();
                if (r.width > 80 && r.height > 18) { el = e; break; }
              }
              if (!el) return false;
              el.focus();
              const opts = {
                key: 'Enter', code: 'Enter', keyCode: 13, which: 13,
                ctrlKey: true, bubbles: true, cancelable: true
              };
              return el.dispatchEvent(new KeyboardEvent('keydown', opts));
            }"""
        )
        if ok:
            return "js:Control+Enter"
    except Exception:
        pass
    return ""


async def _submit(page: Any, *, method: str) -> str:
    """채팅 전송 1회. method: send | ctrl | corner | js | send2."""
    item = await _composer_locator(page)
    if method in ("send", "send2", "corner"):
        how = await _click_send_button(page)
        return how or "send:miss"
    if method in ("ctrl", "js"):
        return await _ctrl_enter(page, item) or "ctrl:miss"
    return await _click_send_button(page) or "send:miss"


async def _composer_still_has_long_text(page: Any, *, min_len: int = 200) -> bool:
    """전송 후에도 입력창에 긴 본문이 남아 있으면 미전송으로 본다."""
    return await _composer_text_len(page) >= min_len


async def _generation_started(page: Any) -> bool:
    txt = await _page_chat_text(page)
    return _page_generation_state(txt or "") == "generating"


async def _submit_ok(page: Any) -> bool:
    """전송 성공: 생성 중이거나, Google 팝업 없이 입력창이 비었을 때."""
    if await _google_account_chooser_visible(page):
        return False
    if await _generation_started(page):
        return True
    if not await _composer_still_has_long_text(page, min_len=400):
        # 입력창만 비고 응답이 전혀 없으면 Google 클릭 오인일 수 있음 — 짧은 대기 후 재확인
        await page.wait_for_timeout(600)
        if await _google_account_chooser_visible(page):
            return False
        if await _generation_started(page):
            return True
        # 페이지에 합본/작성지시가 남아 있으면 아직 미전송
        try:
            body = await page.evaluate(
                "() => (document.body && document.body.innerText || '').slice(0, 20000)"
            )
        except Exception:
            body = ""
        if body and "===== 작성 지시 =====" in body and CHAPTER_START not in (
            extract_chapter_body(body) or ""
        ):
            # 프롬프트만 보이면 전송 실패 가능 — composer 비움만으로 성공 판정하지 않음
            # 단 composer가 비었고 generating이 아니면 잠시 후 scrape가 잡도록 True 유지
            pass
        return True
    return False


async def _submit_ensure(page: Any) -> None:
    """전송 확인 — enter-icon 우선, Ctrl+Enter 보조. Google 팝업이면 실패."""
    if await _google_account_chooser_visible(page):
        raise RuntimeError(
            "Google 로그인/계정 선택 창이 열려 전송할 수 없습니다.\n"
            "팝업에서 계정을 선택한 뒤 「원클릭」을 다시 누르세요."
        )
    methods = ("send", "corner", "ctrl", "send2", "js", "send")
    before = await _composer_text_len(page)
    for attempt, method in enumerate(methods, start=1):
        if await _google_account_chooser_visible(page):
            raise RuntimeError(
                "전송 중 Google 로그인 창이 나타났습니다.\n"
                "계정 선택 후 다시 「원클릭」하세요."
            )
        how = await _submit(page, method=method)
        await page.wait_for_timeout(900)
        after = await _composer_text_len(page)
        ok = await _submit_ok(page)
        grew = after > before + 2
        gen = await _generation_started(page)
        diag_log.log(
            f"전송시도#{attempt} method={method} how={how or '-'} ok={ok} "
            f"composer_len={after} grew={grew} generating={gen}"
        )
        if ok and not grew:
            if gen:
                diag_log.log("전송확인 — 생성 중 감지")
            else:
                diag_log.log("전송확인 — 입력창 비움")
            return
        before = after
        item = await _composer_locator(page)
        if item is not None:
            try:
                await item.click(timeout=1500)
            except Exception:
                pass
    diag_log.log("입력창에 본문 잔존 — 전송 실패")
    raise RuntimeError(
        "합본을 전송하지 못했습니다.\n"
        "Google 로그인 팝업이 떠 있으면 계정을 선택한 뒤 다시 시도하세요.\n"
        "또는 입력창에서 Ctrl+Enter로 전송한 뒤 「결과 파일 → tts」를 사용하세요."
    )


async def _pick_genspark_page(context: Any, url: str) -> Any:
    """CDP 컨텍스트에서 Genspark AI Chat 탭을 고른다."""
    pages = list(getattr(context, "pages", []) or [])
    # agents / ai_chat 우선
    for p in reversed(pages):
        try:
            u = (p.url or "").lower()
        except Exception:
            continue
        if "genspark.ai" in u and ("agents" in u or "ai_chat" in u or "chat" in u):
            try:
                await p.bring_to_front()
            except Exception:
                pass
            return p
    for p in reversed(pages):
        try:
            u = (p.url or "").lower()
        except Exception:
            continue
        if "genspark.ai" in u:
            try:
                await p.bring_to_front()
            except Exception:
                pass
            return p
    page = pages[-1] if pages else await context.new_page()
    await page.goto(url, wait_until="domcontentloaded", timeout=90_000)
    await page.wait_for_timeout(1500)
    return page


async def _wait_chat_ready(page: Any, *, timeout_ms: int = 90_000) -> None:
    """로그인·채팅 UI가 뜰 때까지 대기."""
    deadline = time.time() + timeout_ms / 1000.0
    while time.time() < deadline:
        url = (page.url or "").lower()
        if any(x in url for x in ("login", "signin", "sign-in", "accounts.google")):
            await page.wait_for_timeout(1000)
            continue
        try:
            n = await page.locator(
                "textarea:visible, [contenteditable='true']:visible, "
                "[role='textbox']:visible, input[type='file']"
            ).count()
        except Exception:
            n = 0
        if n > 0 and "genspark.ai" in url:
            await page.wait_for_timeout(800)
            return
        await page.wait_for_timeout(700)
    raise RuntimeError(
        "Genspark 채팅 화면을 찾지 못했습니다.\n"
        "브라우저에서 로그인한 뒤 「실행」를 다시 시도하세요."
    )


def _label_is_claude_opus_46(text: str) -> bool:
    t = (text or "").replace("\n", " ")
    if re.search(r"sonnet", t, re.I):
        return False
    return bool(_TARGET_CHAT_MODEL_RE.search(t))


async def _toolbar_chat_model_label(page: Any) -> str:
    """하단 툴바의 현재 채팅 모델 칩 텍스트."""
    try:
        return str(
            await page.evaluate(
                """() => {
                  const vh = window.innerHeight || 800;
                  const re = /claude|opus|sonnet|gpt|gemini|grok|mixture|deepseek|model|모델/i;
                  const hits = [];
                  const els = document.querySelectorAll(
                    'button, [role="button"], [role="combobox"], [aria-haspopup], div, span'
                  );
                  for (const el of els) {
                    const r = el.getBoundingClientRect();
                    if (r.bottom < vh * 0.55 || r.top > vh - 6) continue;
                    if (r.width < 24 || r.height < 12 || r.height > 64) continue;
                    const t = (el.innerText || el.textContent || '')
                      .replace(/\\s+/g, ' ').trim();
                    if (!t || t.length > 80) continue;
                    if (!re.test(t)) continue;
                    hits.push({ t, h: r.height, bottom: r.bottom });
                  }
                  if (!hits.length) return '';
                  hits.sort((a, b) => a.h - b.h || b.bottom - a.bottom);
                  return hits[0].t;
                }"""
            )
            or ""
        )
    except Exception:
        return ""


async def _open_chat_model_picker(page: Any) -> str:
    """하단 모델 칩을 눌러 목록을 연다."""
    try:
        return str(
            await page.evaluate(
                """() => {
                  const vh = window.innerHeight || 800;
                  const re = /claude|opus|sonnet|gpt|gemini|grok|mixture|deepseek|model|모델/i;
                  const hits = [];
                  const els = document.querySelectorAll(
                    'button, [role="button"], [role="combobox"], [aria-haspopup], div, span'
                  );
                  for (const el of els) {
                    const r = el.getBoundingClientRect();
                    if (r.bottom < vh * 0.55 || r.top > vh - 6) continue;
                    if (r.width < 24 || r.height < 12 || r.height > 64) continue;
                    const t = (el.innerText || el.textContent || '')
                      .replace(/\\s+/g, ' ').trim();
                    if (!t || t.length > 80) continue;
                    if (!re.test(t)) continue;
                    hits.push({ el, t, h: r.height, bottom: r.bottom });
                  }
                  hits.sort((a, b) => a.h - b.h || b.bottom - a.bottom);
                  if (!hits.length) return '';
                  hits[0].el.click();
                  return hits[0].t;
                }"""
            )
            or ""
        )
    except Exception:
        return ""


async def _click_claude_opus_46_option(page: Any) -> str:
    """팝업에서 Claude Opus 4.6 항목 클릭 (Sonnet 제외)."""
    try:
        return str(
            await page.evaluate(
                """() => {
                  const roots = Array.from(document.querySelectorAll(
                    '[role="listbox"], [role="menu"], [role="dialog"], '
                    + '[data-radix-popper-content-wrapper], [class*="popover" i], '
                    + '[class*="dropdown" i], [class*="Menu" i]'
                  ));
                  const scope = roots.length ? roots : [document.body];
                  const nodes = [];
                  for (const root of scope) {
                    nodes.push(...root.querySelectorAll(
                      '[role="option"], [role="menuitem"], button, li, div, span, a'
                    ));
                  }
                  const hits = [];
                  const want = /claude\\s*opus\\s*4\\s*[.\\s]?6|opus\\s*4\\s*[.\\s]?6/i;
                  for (const el of nodes) {
                    const t = (el.innerText || el.textContent || '')
                      .replace(/\\s+/g, ' ').trim();
                    if (!t || t.length > 72) continue;
                    if (/sonnet/i.test(t)) continue;
                    if (!want.test(t)) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width < 20 || r.height < 10 || r.height > 90) continue;
                    hits.push({ el, t, h: r.height, y: r.top });
                  }
                  hits.sort((a, b) => a.h - b.h || a.y - b.y);
                  if (!hits.length) return '';
                  hits[0].el.click();
                  return hits[0].t;
                }"""
            )
            or ""
        )
    except Exception:
        return ""


async def _select_claude_opus_46(page: Any) -> bool:
    """AI Chat 모델 칩에서 Claude Opus 4.6 을 선택한다."""
    before = await _toolbar_chat_model_label(page)
    if _label_is_claude_opus_46(before):
        diag_log.log(f"모델 이미 Opus 4.6: {before[:60]}")
        return True
    diag_log.log(
        f"모델 선택 시작 현재={before[:60] or '(없음)'} → {_TARGET_CHAT_MODEL}"
    )

    opened = await _open_chat_model_picker(page)
    if opened:
        diag_log.log(f"모델 칩 클릭: {opened[:60]}")
        await page.wait_for_timeout(700)
    else:
        for sel in (
            "button:has-text('Model')",
            "button:has-text('모델')",
            "[aria-label*='Model' i]",
            "[aria-label*='모델' i]",
            "[role='combobox']",
        ):
            loc = page.locator(sel).first
            try:
                if not await loc.is_visible(timeout=500):
                    continue
                await loc.click(timeout=3000)
                await page.wait_for_timeout(700)
                opened = "fallback"
                break
            except Exception:
                continue

    picked = await _click_claude_opus_46_option(page)
    if not picked:
        try:
            loc = page.get_by_role(
                "option", name=re.compile(r"Claude\s*Opus\s*4\.?\s*6", re.I)
            ).first
            if await loc.is_visible(timeout=1200):
                label = (await loc.inner_text() or "").strip()
                if not re.search(r"sonnet", label, re.I):
                    await loc.click(timeout=4000)
                    picked = label or _TARGET_CHAT_MODEL
                    await page.wait_for_timeout(500)
        except Exception:
            pass
    if not picked:
        for text in (
            "Claude Opus 4.6",
            "Opus 4.6",
            "claude-opus-4-6",
        ):
            loc = page.locator(
                f"[role='option']:has-text('{text}'), "
                f"[role='menuitem']:has-text('{text}')"
            ).first
            try:
                if not await loc.is_visible(timeout=700):
                    continue
                label = (await loc.inner_text() or "").strip()
                if re.search(r"sonnet", label, re.I):
                    continue
                await loc.click(timeout=4000)
                picked = label or text
                await page.wait_for_timeout(500)
                break
            except Exception:
                continue

    after = await _toolbar_chat_model_label(page)
    ok = _label_is_claude_opus_46(after)
    if not ok and picked and _label_is_claude_opus_46(picked):
        ok = True
    diag_log.log(
        f"모델 선택 결과 ok={ok} 칩={after[:60] or '(없음)'} "
        f"picked={(picked or '')[:40]}"
    )
    return ok


def _iter_targets(page: Any) -> list[Any]:
    """본문 + iframe 대상."""
    out: list[Any] = [page]
    try:
        for fr in page.frames:
            if fr is not page.main_frame:
                out.append(fr)
    except Exception:
        pass
    return out


async def _try_set_files_on_target(target: Any, paths: list[str]) -> bool:
    """숨은 input[type=file] 포함 — accept 제한이 있으면 건너뜀."""
    page = getattr(target, "page", target)
    try:
        await page.evaluate(
            """() => {
              document.querySelectorAll('input[type=file]').forEach(el => {
                try {
                  el.removeAttribute('hidden');
                  el.style.display = 'block';
                  el.style.opacity = '1';
                  el.style.pointerEvents = 'auto';
                } catch (e) {}
              });
            }"""
        )
    except Exception:
        pass
    for sel in ("input[type='file']", "input[type='file'][multiple]"):
        loc = target.locator(sel)
        try:
            n = await loc.count()
        except Exception:
            n = 0
        for i in range(min(n, 16)):
            item = loc.nth(i)
            try:
                await item.wait_for(state="attached", timeout=2000)
                accept = (await item.get_attribute("accept") or "").lower()
                if accept and not any(
                    x in accept
                    for x in (
                        "text",
                        "json",
                        "md",
                        ".txt",
                        ".json",
                        ".md",
                        "*/*",
                        "file",
                    )
                ):
                    # 이미지 전용 등은 스킵
                    if any(
                        x in accept
                        for x in ("image", "png", "jpg", "jpeg", "webp", "gif", "audio", "video")
                    ) and not any(
                        x in accept for x in (".txt", "text", "json", ".md", "*")
                    ):
                        continue
                await item.set_input_files(paths, timeout=15_000)
                await asyncio.sleep(0.6)
                return True
            except Exception as ex:
                diag_log.log(f"set_input_files 실패[{i}]: {ex}")
                continue
    # evaluate 로 숨은 input 강제 탐색
    try:
        handle = await page.evaluate_handle(
            """() => {
              const list = Array.from(document.querySelectorAll('input[type=file]'));
              for (const el of list) {
                const acc = (el.getAttribute('accept') || '').toLowerCase();
                if (acc && /image\\/|audio\\/|video\\//.test(acc)
                    && !/text|json|md|\\*|file/.test(acc)) continue;
                return el;
              }
              return list[0] || null;
            }"""
        )
        el = handle.as_element() if handle else None
        if el is not None:
            await el.set_input_files(paths, timeout=15_000)
            await asyncio.sleep(0.6)
            return True
    except Exception as ex:
        diag_log.log(f"evaluate set_input_files 실패: {ex}")
    return False


async def _click_attach_control(page: Any) -> bool:
    """하단 작곡창 근처 첨부(클립/+) 버튼 클릭."""
    try:
        return bool(
            await page.evaluate(
                """() => {
                  const vh = window.innerHeight || 800;
                  const nodes = Array.from(document.querySelectorAll(
                    'button, [role="button"], label, a, div[role="button"]'
                  ));
                  const hits = [];
                  for (const el of nodes) {
                    const r = el.getBoundingClientRect();
                    if (r.bottom < vh * 0.55 || r.top > vh - 2) continue;
                    if (r.width < 20 || r.height < 20 || r.width > 80) continue;
                    const t = ((el.getAttribute('aria-label') || '')
                      + ' ' + (el.getAttribute('title') || '')
                      + ' ' + (el.innerText || '')).toLowerCase();
                    if (/send|전송|submit|ask|mic|voice|record|모델|model|opus|claude/.test(t))
                      continue;
                    if (/attach|upload|file|파일|첨부|clip|paper|plus|\\+/.test(t)
                        || (!t.trim() && r.width <= 48)) {
                      hits.push({ el, x: r.left, t });
                    }
                  }
                  // 왼쪽(첨부) 우선
                  hits.sort((a, b) => a.x - b.x);
                  if (!hits.length) return false;
                  hits[0].el.click();
                  return true;
                }"""
            )
        )
    except Exception:
        return False


async def _try_filechooser_click(target: Any, paths: list[str]) -> bool:
    page = getattr(target, "page", target)
    # 먼저 하단 첨부 컨트롤
    try:
        async with page.expect_file_chooser(timeout=4000) as fc_info:
            await _click_attach_control(page)
        chooser = await fc_info.value
        await chooser.set_files(paths)
        await asyncio.sleep(0.6)
        return True
    except Exception:
        pass
    for text in (
        "Attach",
        "Upload",
        "첨부",
        "파일 추가",
        "파일",
        "Add file",
        "Add files",
        "업로드",
        "Browse",
    ):
        for sel in (
            f"button:has-text('{text}')",
            f"[role='button']:has-text('{text}')",
            f"[aria-label*='{text}' i]",
            f"[title*='{text}' i]",
            f"label:has-text('{text}')",
        ):
            btn = target.locator(sel).first
            try:
                if not await btn.is_visible(timeout=400):
                    continue
                async with page.expect_file_chooser(timeout=8_000) as fc_info:
                    await btn.click(timeout=4000)
                chooser = await fc_info.value
                await chooser.set_files(paths)
                await asyncio.sleep(0.6)
                return True
            except Exception:
                continue
    for sel in (
        "[data-testid*='attach' i]",
        "[data-testid*='upload' i]",
        "[aria-label*='attach' i]",
        "[aria-label*='upload' i]",
        "[aria-label*='file' i]",
        "[aria-label*='클립' i]",
        "[aria-label*='첨부' i]",
        "label:has(input[type='file'])",
    ):
        btns = target.locator(sel)
        try:
            n = await btns.count()
        except Exception:
            n = 0
        for i in range(min(n, 12)):
            btn = btns.nth(i)
            try:
                if not await btn.is_visible(timeout=300):
                    continue
                async with page.expect_file_chooser(timeout=6000) as fc_info:
                    await btn.click(timeout=3000)
                chooser = await fc_info.value
                await chooser.set_files(paths)
                await asyncio.sleep(0.6)
                return True
            except Exception:
                continue
    return False



async def _attach_files(page: Any, files: list[Path]) -> bool:
    """input[type=file] / filechooser — 일괄 → 개별 → 번들."""
    paths = [str(Path(p).resolve()) for p in files if Path(p).is_file()]
    if not paths:
        raise RuntimeError("첨부할 파일이 없습니다.")

    await _wait_chat_ready(page)
    try:
        n_inp = await page.locator("input[type='file']").count()
        diag_log.log(f"첨부시작 files={len(paths)} file_inputs={n_inp}")
    except Exception:
        diag_log.log(f"첨부시작 files={len(paths)}")

    async def _once(target_paths: list[str]) -> bool:
        # 첨부 버튼으로 input 노출 유도
        await _click_attach_control(page)
        await page.wait_for_timeout(250)
        for target in _iter_targets(page):
            if await _try_set_files_on_target(target, target_paths):
                return True
        for target in _iter_targets(page):
            if await _try_filechooser_click(target, target_paths):
                return True
        return False

    for attempt in range(4):
        if await _once(paths):
            diag_log.log(f"첨부성공(일괄) n={len(paths)} attempt={attempt + 1}")
            return True
        try:
            await page.locator(
                "textarea:visible, [contenteditable='true']:visible"
            ).first.click(timeout=2000)
        except Exception:
            pass
        await page.wait_for_timeout(400)

    ok_n = 0
    for path in paths:
        attached = False
        for _attempt in range(2):
            if await _once([path]):
                attached = True
                break
            await page.wait_for_timeout(300)
        if attached:
            ok_n += 1
            diag_log.log(f"첨부성공(개별) {Path(path).name}")
            await page.wait_for_timeout(300)
        else:
            diag_log.log(f"첨부실패(개별) {Path(path).name}")
    if ok_n > 0:
        diag_log.log(f"첨부부분성공 {ok_n}/{len(paths)}")
        return True
    diag_log.log(f"첨부실패 전부 n={len(paths)}")
    return False


def _read_text_file(path: Path, *, limit: int = 400_000) -> str:
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if len(raw) > limit:
        return raw[:limit] + "\n…(이하 생략)"
    return raw


def _page_generation_state(txt: str) -> str:
    """generating | interrupted | idle."""
    t = txt or ""
    if re.search(
        r"요청이\s*중단|중단되었습니다|stopped|Stop\s*generating|생성\s*중|답변\s*작성|Thinking|응답\s*생성",
        t,
        flags=re.IGNORECASE,
    ):
        if re.search(r"요청이\s*중단|중단되었습니다|stopped", t, flags=re.IGNORECASE):
            return "interrupted"
        return "generating"
    return "idle"


def _response_looks_finished(
    txt: str, *, min_chars: int = 2500, original: str = ""
) -> bool:
    """응답 완료: 생성 종료 + CHAPTER_START/END 본문 충분."""
    del original
    if not txt:
        return False
    if _page_generation_state(txt) != "idle":
        return False
    body = extract_chapter_body(txt)
    if not body:
        return False
    return len(body) >= min_chars



async def _page_chat_text(page: Any) -> str:
    """채팅 DOM에서 장 대본 응답 텍스트 수집."""
    try:
        return await page.evaluate(
            """() => {
              const parts = [];
              const nodes = Array.from(document.querySelectorAll('*'));
              for (const el of nodes) {
                try {
                  if (el.scrollHeight > el.clientHeight + 80) {
                    el.scrollTop = el.scrollHeight;
                  }
                } catch (e) {}
              }
              window.scrollTo(0, document.body.scrollHeight);
              const push = (t) => {
                const s = (t || '').trim();
                if (s.length > 40) parts.push(s);
              };
              push(document.body && document.body.innerText);
              for (const sel of [
                'pre', 'code',
                '[class*="markdown" i]', '[class*="message" i]',
                '[class*="assistant" i]', '[class*="prose" i]',
                '[class*="response" i]', '[class*="answer" i]',
                '[data-testid*="message" i]', 'article'
              ]) {
                for (const el of document.querySelectorAll(sel)) {
                  push(el.innerText || el.textContent || '');
                }
              }
              const start = '<<<CHAPTER_START>>>';
              const end = '<<<CHAPTER_END>>>';
              const score = (p) => {
                const hasM = p.includes(start) && p.includes(end) ? 500 : 0;
                return hasM + Math.min(p.length, 80000) / 1000;
              };
              const hasSignal = (p) => p.includes(start) || p.includes(end);
              const pool = parts.filter(hasSignal);
              if (!pool.length) return parts.sort((a, b) => b.length - a.length)[0] || '';
              pool.sort((a, b) => score(b) - score(a));
              return pool[0] || '';
            }"""
        )
    except Exception:
        return ""


def _dump_scrape_page(txt: str, *, tag: str = "fail") -> None:
    """파싱 실패 시 페이지 텍스트를 남겨 재현·수정에 쓴다."""
    try:
        base = diag_log.log_path()
        if base is None:
            return
        dump = base.with_name(f"plot_to_prompt_page_{tag}.txt")
        dump.write_text(txt or "", encoding="utf-8")
        diag_log.log(f"페이지덤프 → {dump} chars={len(txt or '')}")
    except OSError:
        pass


async def _scrape_chapter_text(
    page: Any,
    *,
    wait_ms: int | None = None,
    min_chars: int = 2500,
) -> str:
    """응답 장 대본(CHAPTER_START/END) 자동 추출 — 최신 블록 + 「복사하기」.

    같은 채팅에 이전 장 대본이 남아 있어도, 스크래프 시작 시점의 블록은 무시한다.
    """
    if wait_ms is None:
        wait_ms = 900_000
    deadline = time.time() + wait_ms / 1000.0

    txt0 = await _page_chat_text(page)
    ignore = frozenset(_iter_marker_bodies(txt0 or ""))
    if ignore:
        diag_log.log(
            f"스크래프 기준 이전블록 n={len(ignore)} "
            f"lens={[len(b) for b in ignore]}"
        )

    best = ""
    stable_hits = 0
    page_stable = 0
    last_page_len = -1
    last_body_len = -1
    body_stable = 0
    copy_tried_at = 0
    kb_tried = False
    tick = 0
    interrupted_hits = 0
    dumped = False
    saw_new_end = False

    diag_log.log(f"스크래프시작 wait_ms={wait_ms} min_chars={min_chars}")

    while time.time() < deadline:
        txt = await _page_chat_text(page)
        tick += 1
        page_len = len(txt or "")
        gen = _page_generation_state(txt or "")
        markers = bool(txt and CHAPTER_START in txt and CHAPTER_END in txt)
        body = extract_chapter_body(txt, ignore_bodies=ignore) if txt else None
        body_len = len(body or "")
        ok = body_len >= min_chars
        if body_len > 0:
            saw_new_end = True

        if page_len == last_page_len:
            page_stable += 1
        else:
            page_stable = 0
            last_page_len = page_len
        if body_len == last_body_len and body_len > 0:
            body_stable += 1
        else:
            body_stable = 0
            last_body_len = body_len

        finished = (
            gen == "idle"
            and ok
            and body_stable >= 2
            and (
                page_stable >= 2
                or body_stable >= 3
            )
            and _response_looks_finished(
                # finished check on the extracted body context
                f"{CHAPTER_START}\n{body}\n{CHAPTER_END}" if body else (txt or ""),
                min_chars=min_chars,
            )
        )
        if gen == "interrupted":
            interrupted_hits += 1
        else:
            interrupted_hits = 0
        if tick == 1 or tick % 10 == 0 or finished or gen != "idle" or (
            ok and body_stable >= 1
        ):
            diag_log.log(
                f"스크래프 tick={tick} chars={page_len} body={body_len} "
                f"gen={gen} page_stable={page_stable} body_stable={body_stable} "
                f"ok={ok} finished={finished} markers={markers} "
                f"new={saw_new_end} best={len(best)}"
            )
            if markers and body_len == 0 and not dumped and page_stable >= 5:
                _dump_scrape_page(txt or "", tag="nobody")
                dumped = True

        if gen == "generating" and not ok:
            stable_hits = 0
            await page.wait_for_timeout(2000)
            continue
        if gen == "interrupted" and interrupted_hits >= 2 and not ok:
            await page.wait_for_timeout(2500)
            continue

        if body and body_len > 0:
            if body != best:
                best = body
                stable_hits = 1
            else:
                stable_hits += 1
            if ok and finished and stable_hits >= 1:
                diag_log.log(f"스크래프완료 chars={len(best)}")
                return best
            if ok and gen == "idle" and body_stable >= 3 and stable_hits >= 2:
                diag_log.log(f"스크래프완료(안정) chars={len(best)}")
                return best

        # CHAPTER_END(신규) 보이면 「복사하기」 자동 — DOM 추출 실패·지연 대비
        want_copy = (
            gen == "idle"
            and markers
            and tick >= 3
            and (tick - copy_tried_at >= 5 or copy_tried_at == 0)
            and (
                (saw_new_end and body_stable >= 2)
                or (page_stable >= 3 and not ok)
                or (ok and body_stable >= 2 and tick - copy_tried_at >= 10)
            )
        )
        if want_copy:
            copy_tried_at = tick
            diag_log.log("복사하기 버튼 자동 클릭 시도")
            copied = await _try_click_copy_near_end(page)
            if copied:
                b = extract_chapter_body(copied, ignore_bodies=ignore)
                if not b and not looks_like_genspark_packet(copied):
                    raw = copied.strip()
                    if len(raw) >= min_chars:
                        b = raw
                if b and len(b) > len(best):
                    best = b
                    diag_log.log(f"복사버튼추출 chars={len(best)}")
                if best and len(best) >= min_chars and (
                    extract_chapter_body(copied, ignore_bodies=ignore) == best
                    or len(best) >= min_chars
                ):
                    # 복사본이 신규 블록이면 즉시 완료
                    if best not in ignore and len(best) >= min_chars:
                        diag_log.log(f"스크래프완료(복사) chars={len(best)}")
                        return best

        if (
            not ok
            and page_stable >= 12
            and gen == "idle"
            and not kb_tried
            and tick >= 12
        ):
            kb_tried = True
            copied = await _keyboard_copy_page(page)
            if copied:
                b = extract_chapter_body(copied, ignore_bodies=ignore)
                if b and len(b) > len(best):
                    best = b
                    diag_log.log(
                        f"키보드복사시도 chars={len(copied)} body={len(best)}"
                    )
                    if len(best) >= min_chars and best not in ignore:
                        diag_log.log(f"스크래프완료(키보드) chars={len(best)}")
                        return best

        if tick >= 25 and page_stable >= 15 and not best:
            if await _google_account_chooser_visible(page):
                diag_log.log("스크래프대기 — Google 팝업 감지, 계속 대기")
                await page.wait_for_timeout(2500)
                continue
            still = await _composer_still_has_long_text(page, min_len=400)
            if still:
                diag_log.log(
                    "스크래프중단 — 입력창에 본문이 남아 있어 미전송으로 판단"
                )
                return ""
            if page_len < 500:
                diag_log.log(
                    f"스크래프중단 — 응답 없음 tick={tick} chars={page_len}"
                )
                return ""
        await page.wait_for_timeout(1500)

    if best and len(best) >= min_chars and best not in ignore:
        diag_log.log(f"스크래프종료 best chars={len(best)}")
        return best
    diag_log.log(f"스크래프결과 부족 best={len(best)} min={min_chars}")
    return best if best not in ignore else ""



async def _try_click_copy_near_end(page: Any) -> str:
    """응답 하단 「복사/복사하기」를 눌러 클립보드에서 대본 읽기."""
    clicked = False
    # 1) Playwright 로케이터 — 젠스파크 UI 문구 우선
    for sel in (
        "button:has-text('복사하기')",
        "[role='button']:has-text('복사하기')",
        "button:has-text('복사')",
        "[role='button']:has-text('복사')",
        "button:has-text('Copy')",
        "[aria-label*='복사' i]",
        "[aria-label*='Copy' i]",
        "[title*='복사' i]",
        "[title*='Copy' i]",
    ):
        try:
            loc = page.locator(sel)
            n = await loc.count()
            if n <= 0:
                continue
            # 마지막(보통 최신 응답) 버튼
            btn = loc.nth(n - 1)
            if await btn.is_visible(timeout=400):
                await btn.click(timeout=4000, force=True)
                clicked = True
                diag_log.log(f"복사버튼 클릭 sel={sel} nth={n - 1}")
                break
        except Exception:
            continue

    # 2) DOM 스캔 — CHAPTER 마커가 있는 응답 근처 복사 버튼
    if not clicked:
        try:
            clicked = bool(
                await page.evaluate(
                    """() => {
                      const start = '<<<CHAPTER_START>>>';
                      const end = '<<<CHAPTER_END>>>';
                      const html = (document.body && document.body.innerText) || '';
                      if (!html.includes(start) || !html.includes(end)) return false;
                      const all = Array.from(
                        document.querySelectorAll(
                          'button, [role="button"], a, [class*="copy" i], [class*="Copy" i]'
                        )
                      );
                      const isCopy = (el) => {
                        const t = ((el.innerText || '') + ' '
                          + (el.getAttribute('aria-label') || '') + ' '
                          + (el.getAttribute('title') || '') + ' '
                          + (el.className && String(el.className) || '')
                        ).toLowerCase();
                        if (/download|다운로드|share|공유|like|좋아요|attach|첨부/.test(t))
                          return false;
                        return /copy|복사하기|복사|clipboard/.test(t);
                      };
                      // 아래에서 위로 — 최신 응답의 복사 버튼
                      for (let i = all.length - 1; i >= 0; i--) {
                        const el = all[i];
                        if (!isCopy(el)) continue;
                        const r = el.getBoundingClientRect();
                        if (r.width < 8 || r.height < 8) continue;
                        try { el.click(); return true; } catch (e) {}
                      }
                      return false;
                    }"""
                )
            )
            if clicked:
                diag_log.log("복사버튼 클릭 how=dom-scan")
        except Exception as ex:
            diag_log.log(f"복사버튼 DOM 실패: {ex!r}")
            clicked = False

    if not clicked:
        return ""
    await page.wait_for_timeout(600)
    clip = ""
    try:
        clip = await page.evaluate(
            """async () => {
              try { return await navigator.clipboard.readText(); }
              catch (e) { return ''; }
            }"""
        )
    except Exception:
        clip = ""
    clip = (clip or "").strip()
    if clip:
        return clip
    # 클립보드 권한 실패 시 페이지에서 다시 추출
    try:
        return (await _page_chat_text(page) or "").strip()
    except Exception:
        return ""


async def _keyboard_copy_page(page: Any) -> str:
    """본문 포커스 후 Ctrl+A / Ctrl+C 로 클립보드 수집 (권한 있을 때)."""
    try:
        await page.evaluate(
            """() => {
              const el = document.body;
              if (!el) return;
              const range = document.createRange();
              range.selectNodeContents(el);
              const sel = window.getSelection();
              sel.removeAllRanges();
              sel.addRange(range);
            }"""
        )
        await page.keyboard.press("Control+C")
        await page.wait_for_timeout(400)
        clip = await page.evaluate(
            """async () => {
              try { return await navigator.clipboard.readText(); }
              catch (e) { return ''; }
            }"""
        )
        return (clip or "").strip()
    except Exception:
        return ""



class GensparkChatSession:
    def __init__(self, profile_dir: Path) -> None:
        self._profile_dir = profile_dir
        self._cmd_q: queue.Queue[tuple[str, Any, queue.Queue]] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._worker_main, daemon=True)
            self._thread.start()

    def _worker_main(self) -> None:
        asyncio.run(self._async_worker())

    def _call(self, op: str, arg: Any = None, *, timeout: float = 300.0) -> Any:
        self._ensure_thread()
        resp_q: queue.Queue[tuple[bool, Any, Exception | None]] = queue.Queue()
        self._cmd_q.put((op, arg, resp_q))
        try:
            ok, result, err = resp_q.get(timeout=timeout)
        except queue.Empty as e:
            raise TimeoutError("Genspark 작업 시간이 초과되었습니다.") from e
        if not ok and err:
            raise err
        return result

    def open_chat(
        self,
        *,
        url: str = GENSPARK_AI_CHAT_URL,
        email: str = "",
        password: str = "",
    ) -> dict[str, bool]:
        return self._call(
            "open",
            {"url": url, "email": email, "password": password},
            timeout=180.0,
        )

    def run_chapter(
        self,
        *,
        packet_text: str,
        url: str = GENSPARK_AI_CHAT_URL,
        email: str = "",
        password: str = "",
    ) -> dict[str, Any]:
        return self._call(
            "chapter",
            {
                "url": url,
                "packet_text": packet_text,
                "email": email,
                "password": password,
            },
            timeout=960.0,
        )


    def stop(self) -> None:
        try:
            self._call("stop", timeout=10.0)
        except Exception:
            pass

    async def _async_worker(self) -> None:
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            context = await _launch_context(pw, self._profile_dir)
            try:
                for origin in (
                    "https://www.genspark.ai",
                    "https://genspark.ai",
                    "https://www.genspark.ai/",
                ):
                    try:
                        await context.grant_permissions(
                            ["clipboard-read", "clipboard-write"],
                            origin=origin,
                        )
                    except Exception:
                        pass
            except Exception:
                pass
            page = await _pick_genspark_page(context, GENSPARK_AI_CHAT_URL)

            while True:
                try:
                    op, arg, resp_q = self._cmd_q.get_nowait()
                except queue.Empty:
                    await asyncio.sleep(0.15)
                    continue
                try:
                    if op == "open":
                        data = arg or {}
                        url = data.get("url") or GENSPARK_AI_CHAT_URL
                        email = str(data.get("email") or "")
                        password = str(data.get("password") or "")
                        page = await _pick_genspark_page(context, url)
                        cur = (page.url or "").lower()
                        if "genspark.ai" not in cur or "agents" not in cur:
                            await page.goto(
                                url, wait_until="domcontentloaded", timeout=90_000
                            )
                            await page.wait_for_timeout(500)
                        login_info = await _ensure_login(
                            page, email, password, context=context
                        )
                        if login_info.get("logged_in"):
                            try:
                                await _wait_chat_ready(page, timeout_ms=30_000)
                                await _select_claude_opus_46(page)
                            except Exception as ex:
                                diag_log.log(f"open 모델 선택 경고: {ex}")
                        resp_q.put(
                            (
                                True,
                                {
                                    "ok": True,
                                    "logged_in": bool(login_info.get("logged_in")),
                                    "login_attempted": bool(
                                        login_info.get("attempted")
                                    ),
                                },
                                None,
                            )
                        )
                    elif op == "chapter":
                        data = arg or {}
                        url = data.get("url") or GENSPARK_AI_CHAT_URL
                        email = str(data.get("email") or "")
                        password = str(data.get("password") or "")
                        packet = str(data.get("packet_text") or "")
                        if not packet.strip():
                            raise RuntimeError("합본 텍스트가 비어 있습니다.")
                        page = await _pick_genspark_page(context, url)
                        cur = (page.url or "").lower()
                        if "genspark.ai" not in cur or "agents" not in cur:
                            await page.goto(
                                url,
                                wait_until="domcontentloaded",
                                timeout=90_000,
                            )
                            await page.wait_for_timeout(600)
                        login_info = await _ensure_login(
                            page, email, password, context=context
                        )
                        if email and password and not login_info.get("logged_in"):
                            login_info = await _ensure_login(
                                page,
                                email,
                                password,
                                context=context,
                                force=True,
                            )
                        if not await _clear_google_blocker(
                            page,
                            email=email,
                            password=password,
                            context=context,
                        ):
                            raise RuntimeError(
                                "Google 로그인/계정 선택 창이 열려 있습니다.\n"
                                "팝업에서 계정을 선택(또는 비밀번호 입력)한 뒤\n"
                                "「원클릭: 대본 생성」을 다시 누르세요.\n"
                                f"(ChromeDebug: C:\\ChromeDebug_1_3 · 포트 {_CDP_PORT})"
                            )
                        if not login_info.get("logged_in") and await _login_wall_visible(
                            page
                        ):
                            raise RuntimeError(
                                "Genspark 자동 로그인에 실패했습니다.\n"
                                "1) GUI에 Google 이메일·비밀번호를 넣었는지 확인\n"
                                "2) 브라우저에서 「Google로 계속하기」로 한 번 로그인한 뒤\n"
                                "   같은 Chrome(C:\\ChromeDebug_1_3)을 유지한 채 다시 실행"
                            )
                        await _wait_chat_ready(page, timeout_ms=45_000)
                        try:
                            await _select_claude_opus_46(page)
                        except Exception as ex:
                            diag_log.log(f"모델 선택 경고: {ex}")
                        diag_log.log(f"합본텍스트 입력 chars={len(packet)}")
                        if not await _fill_first_editable(page, packet):
                            raise RuntimeError(
                                "합본 텍스트를 입력창에 넣지 못했습니다."
                            )
                        # 입력 직후 Google 팝업이 뜨는 경우
                        if not await _clear_google_blocker(
                            page,
                            email=email,
                            password=password,
                            context=context,
                        ):
                            raise RuntimeError(
                                "합본 입력 후 Google 로그인 창이 나타났습니다.\n"
                                "계정 선택 후 「원클릭: 대본 생성」을 다시 누르세요."
                            )
                        await page.wait_for_timeout(400)
                        await _submit_ensure(page)
                        still = await _composer_still_has_long_text(page, min_len=400)
                        started = await _generation_started(page)
                        diag_log.log(
                            f"작성제출 composer_remain={still} generating={started}"
                        )
                        if still and not started:
                            # 한 번 더: 팝업 치우고 재전송
                            await _clear_google_blocker(
                                page,
                                email=email,
                                password=password,
                                context=context,
                            )
                            await _submit_ensure(page)
                            still = await _composer_still_has_long_text(
                                page, min_len=400
                            )
                            started = await _generation_started(page)
                            diag_log.log(
                                f"작성재전송 composer_remain={still} generating={started}"
                            )
                        if still and not started:
                            raise RuntimeError(
                                "합본이 입력창에 남아 전송되지 않았습니다.\n"
                                "Google 로그인 팝업을 닫고 「원클릭」을 다시 누르거나,\n"
                                "입력창에서 Ctrl+Enter 후 결과가 나오면 「결과 파일 → tts」."
                            )
                        # 생성 시작 대기 (전송 직후 idle인 경우)
                        if not started:
                            for _ in range(15):
                                await page.wait_for_timeout(1000)
                                if await _generation_started(page):
                                    started = True
                                    break
                                body0 = extract_chapter_body(
                                    await _page_chat_text(page) or ""
                                )
                                if body0 and len(body0) >= 800:
                                    break
                            diag_log.log(f"생성대기 후 generating={started}")
                        scraped = await _scrape_chapter_text(page, min_chars=2500)
                        diag_log.log(
                            f"스크래프결과 chars={len(scraped or '')} "
                            f"head={diag_log.preview(scraped or '')}"
                        )
                        resp_q.put(
                            (
                                True,
                                {
                                    "ok": True,
                                    "chapter_text": scraped or "",
                                    "logged_in": bool(login_info.get("logged_in")),
                                    "login_attempted": bool(
                                        login_info.get("attempted")
                                    ),
                                },
                                None,
                            )
                        )
                    elif op == "stop":
                        resp_q.put((True, None, None))
                        break
                    else:
                        resp_q.put(
                            (False, None, RuntimeError(f"알 수 없는 명령: {op}"))
                        )
                except Exception as ex:
                    resp_q.put((False, None, ex))


_session: GensparkChatSession | None = None
_session_lock = threading.Lock()


def reset_session() -> None:
    global _session
    with _session_lock:
        if _session is not None:
            try:
                _session.stop()
            except Exception:
                pass
            _session = None


def get_chat_session(profile_dir: Path) -> GensparkChatSession:
    global _session
    with _session_lock:
        if _session is None:
            _session = GensparkChatSession(profile_dir)
        return _session


def run_chapter_flow(
    *,
    packet_text: str,
    profile_dir: Path,
    open_browser: bool = True,
    url: str = GENSPARK_AI_CHAT_URL,
    email: str = "",
    password: str = "",
) -> dict[str, Any]:
    """브라우저 오픈(옵션) → 로그인 → 합본 붙여넣기·전송 → 장 대본 스크랩."""
    if not has_playwright():
        raise RuntimeError("Playwright가 필요합니다.\npip install playwright")
    packet = (packet_text or "").strip()
    if not packet:
        raise RuntimeError("합본 텍스트가 비어 있습니다.")
    if open_browser:
        already = wait_cdp_ready(timeout_sec=1.0)
        info = open_chrome_debug(url, restart=not already)
        diag_log.log(
            f"ChromeDebug reused={info.get('reused')} "
            f"port={info.get('debug_port')} restart={not already}"
        )
        if not already:
            time.sleep(0.3)
    sess = get_chat_session(profile_dir)
    return sess.run_chapter(
        packet_text=packet,
        url=url,
        email=email,
        password=password,
    )

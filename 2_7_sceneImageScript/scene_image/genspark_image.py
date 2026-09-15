# -*- coding: utf-8 -*-
"""Genspark AI Image — Nano banana pro 선택·프롬프트 전송·이미지 수집 (Playwright)."""

from __future__ import annotations

import asyncio
import base64
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

from scene_image.chrome_slot import (
    ensure_chrome_slot,
    get_active_slot,
    release_chrome_slot,
)
from scene_image.limit_detect import (
    AiImageLimitError,
    BROWSER_CLOSED_MSG,
    BrowserClosedError,
    detect_limit_on_page,
    is_browser_closed_error,
    limit_hit_from_text,
    parse_reset_at,
    raise_limit_error,
    read_page_visible_text,
)
from scene_image.paths import GENSPARK_AI_IMAGE_URL
from scene_image.scene_parse import (
    find_previous_reference_png,
    png_already_exists,
    previous_reference_slot_sec,
    reference_png_path_ok,
    resolve_strict_reference_png,
    scene_location_fingerprint,
    scene_png_path,
    srt_png_name,
)
from scene_image.url_filter import (
    is_collectable_image_url,
    is_genspark_file_url,
    is_tracking_url,
    looks_like_image_url,
    normalize_genspark_file_url,
)

_NANO_BANANA_PRO_TEXTS = (
    "Nano Banana Pro",
    "Nano banana pro",
    "nano banana pro",
    "NanoBanana Pro",
    "NanoBananaPro",
    "Banana Pro",
)
_PROFILE_DIRNAME = ".genspark_scene_image_profile"
_STORAGE_STATE_NAME = "storage_state.json"
# 모듈 전용 ChromeDebug — 슬롯 N: port 9242+N, C:\ChromeDebug_2_5_slotN
# (인스턴스 동시 실행 시 chrome_slot.ensure_chrome_slot 이 자동 할당)
_CDP_PORT = 9242
_CHROME_DEBUG_USER_DATA = Path(r"C:\ChromeDebug_2_5")


def _slot_defaults() -> tuple[int, Path]:
    """활성 슬롯의 (port, user_data). 없으면 확보 후 반환."""
    slot = get_active_slot() or ensure_chrome_slot()
    return int(slot.port), Path(slot.user_data)
_GENSPARK_FILE_RE = re.compile(
    r"https?://(?:www\.)?genspark\.ai/api/files/[^\s\"'<>]+",
    re.IGNORECASE,
)
_SRT_LABEL_RE = re.compile(r"SRT[_\s-]?(\d{1,6})", re.IGNORECASE)
# True 이면 filechooser 가드가 첨부를 가로채지 않음
_FC_ALLOW: dict[str, bool] = {"v": False}
# 참조 첨부 중 작업 탭 — NEW_PAGE 승격·새 창 전환 방지
_ATTACH_KEEP_PAGE: dict[str, Any] = {"page": None}
_ATTACH_WORK_URL: dict[str, str] = {"v": ""}
# 직전 씬 다운로드후유휴 — 다음 씬 ``다음명령유휴`` 중복 생략
_LAST_SCENE_IDLE: dict[str, float | bool] = {"at": 0.0, "ok": False}
# 직전 씬 저장 결과 — 참조 첨부 시 직전 PNG·URL 고정
_LAST_SCENE_REF: dict[str, Any] = {"sec": None, "path": "", "file_url": ""}
# 같은 장소 연속 N회부터 참조 첨부 생략 (2 = 두 번째 연속부터 OFF)
_REF_LOCATION_STREAK: dict[str, Any] = {"fp": "", "count": 0}
REF_SAME_LOCATION_MAX = 2
# 성공 문구만(이미지 없음) — 재첨부·재전송 최대 횟수
PHANTOM_RESUBMIT_MAX = 1
ATTACH_SUBMIT_DELAY_MS = 1000
# 탭/창 디버그 → image.log (png 형제 log/)
_TAB_LOG_PNG: dict[str, Path | None] = {"dir": None}


def set_tab_log_png_dir(png_dir: Path | None) -> None:
    """탭·창 이벤트를 image.log 에 남길 png 폴더 지정."""
    _TAB_LOG_PNG["dir"] = Path(png_dir) if png_dir else None


def _tab_log(message: str) -> None:
    d = _TAB_LOG_PNG.get("dir")
    if d is None:
        return
    try:
        from scene_image.image_log import append_image_log

        append_image_log(d, message)
    except Exception:
        pass


def _timing_sec(t0: float) -> float:
    return round(max(0.0, time.perf_counter() - t0), 1)


def _timing_log(label: str, t0: float, *, extra: str = "") -> float:
    """단계 소요(초)를 image.log 에 남기고 새 시각을 반환."""
    sec = _timing_sec(t0)
    msg = f"⏱ {label} {sec}s"
    if extra:
        msg = f"{msg} · {extra}"
    _tab_log(msg)
    return time.perf_counter()


async def _tab_snapshot(context: Any, keep: Any | None = None) -> str:
    """현재 탭 목록 한 줄 요약."""
    lines: list[str] = []
    try:
        pages = list(getattr(context, "pages", []) or [])
    except Exception:
        return "(tabs: ?)"
    for i, p in enumerate(pages):
        try:
            closed = p.is_closed()
            u = "" if closed else (p.url or "")[:120]
            mark = "*" if keep is not None and p is keep else " "
            lines.append(f"{mark}[{i}]{'CLOSED' if closed else u}")
        except Exception as ex:
            lines.append(f" [{i}]err:{ex}")
    return f"tabs={len(pages)} " + " | ".join(lines)


def storage_state_path(base_dir: Path) -> Path:
    """Playwright storageState 경로 (세션·쿠키 유지)."""
    return Path(base_dir) / _STORAGE_STATE_NAME


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


def _subprocess_no_window_flags() -> int:
    """Windows: PowerShell 등 보조 프로세스 콘솔 창 숨김."""
    if sys.platform != "win32":
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def close_chrome_debug(*, user_data_dir: Path | None = None) -> None:
    """이 슬롯 ChromeDebug(CDP)만 종료 — 다른 슬롯·모듈은 건드리지 않음."""
    reset_image_session()
    if user_data_dir is not None:
        data_path = Path(user_data_dir)
    else:
        slot = get_active_slot()
        if slot is None:
            return
        data_path = Path(slot.user_data)
    data = str(data_path.resolve()).rstrip("\\/")
    data_esc = data.replace("'", "''")
    if sys.platform == "win32":
        # --user-data-dir=경로 정확 매칭 (접두사 충돌 방지: …_2_5 vs …_2_5_slot1)
        ps = (
            "$ud='"
            + data_esc
            + "';"
            "$esc=[regex]::Escape($ud);"
            "$re=('(?i)--user-data-dir=\"?'+$esc+'\"?(?:\\s|$)');"
            "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
            "ForEach-Object {"
            "  $cl=$_.CommandLine; if(-not $cl){return};"
            "  if($cl -match $re){"
            "    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue"
            "  }"
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
                creationflags=_subprocess_no_window_flags(),
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    time.sleep(0.8)


def clear_chrome_session_restore(user_data_dir: Path | None = None) -> None:
    """이전 창·탭 복원을 막아 「실행」 때 매직 다시 그리기 등이 재등장하지 않게."""
    if user_data_dir is not None:
        root = Path(user_data_dir)
    else:
        slot = get_active_slot()
        if slot is None:
            root = _CHROME_DEBUG_USER_DATA
        else:
            root = Path(slot.user_data)
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
    for sub in default.glob("Sessions/*"):
        try:
            if sub.is_file():
                sub.unlink()
            elif sub.is_dir():
                shutil.rmtree(sub, ignore_errors=True)
        except OSError:
            pass
    pref_path = default / "Preferences"
    if pref_path.is_file():
        try:
            import json

            raw = pref_path.read_text(encoding="utf-8")
            data = json.loads(raw)
            session = data.setdefault("session", {})
            session["restore_on_startup"] = 5
            data["session"] = session
            pref_path.write_text(
                json.dumps(data, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
        except (OSError, ValueError, TypeError):
            pass


def image_profile_dir(base_dir: Path) -> Path:
    """레거시 전용 프로필 (계정 Chrome 프로필을 쓸 때는 사용하지 않음)."""
    return base_dir / _PROFILE_DIRNAME


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
                    time.sleep(0.6)
                    return True
        except (urllib.error.URLError, TimeoutError, OSError):
            time.sleep(0.4)
    return False


def open_chrome_debug(
    url: str = GENSPARK_AI_IMAGE_URL,
    *,
    debug_port: int | None = None,
    user_data_dir: Path | None = None,
    restart: bool = False,
) -> dict[str, str]:
    """슬롯별 디버그 Chrome 실행.

    슬롯 N: ``--remote-debugging-port=9242+N --user-data-dir=C:\\ChromeDebug_2_5_slotN``
    ``restart=True`` 이면 이 슬롯 Chrome만 종료한 뒤 다시 연다.
    이미 CDP 가 떠 있으면 재사용한다 (restart 제외).
    """
    slot_port, slot_ud = _slot_defaults()
    port = int(debug_port if debug_port is not None else slot_port)
    data_dir = Path(user_data_dir or slot_ud)
    if restart:
        close_chrome_debug(user_data_dir=data_dir)
        clear_chrome_session_restore(data_dir)
    elif wait_cdp_ready(debug_port=port, timeout_sec=1.5):
        return {
            "mode": "chrome_debug",
            "debug_port": str(port),
            "user_data": str(data_dir.resolve()),
            "reused": "1",
            "slot": str((get_active_slot() or ensure_chrome_slot()).index),
        }
    clear_chrome_session_restore(data_dir)
    chrome = find_chrome_exe()
    if chrome is None:
        raise RuntimeError(
            "Google Chrome을 찾을 수 없습니다.\nChrome 설치 후 다시 시도하세요."
        )
    data_dir.mkdir(parents=True, exist_ok=True)
    reset_image_session()
    # URL은 Playwright가 연 뒤에 이동 — 창만 뜨고 자동화가 안 붙는 착시 방지
    del url  # 호환 인자 (시작 URL은 세션 open_model 에서 처리)
    args: list[str] = [
        str(chrome),
        f"--remote-debugging-port={port}",
        f"--user-data-dir={data_dir.resolve()}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-session-crashed-bubble",
        "--disable-infobars",
        "--new-window",
        "about:blank",
    ]
    kwargs: dict = {"args": args, "close_fds": True}
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    subprocess.Popen(**kwargs)
    if not wait_cdp_ready(debug_port=port, timeout_sec=45.0):
        raise RuntimeError(
            f"ChromeDebug(CDP :{port})에 연결하지 못했습니다.\n"
            "Chrome이 완전히 뜬 뒤 「실행」를 다시 눌러 주세요."
        )
    return {
        "mode": "chrome_debug",
        "debug_port": str(port),
        "user_data": str(data_dir.resolve()),
        "reused": "0",
        "slot": str((get_active_slot() or ensure_chrome_slot()).index),
    }


def open_genspark_in_chrome(
    url: str = GENSPARK_AI_IMAGE_URL,
    *,
    profile_dir: Path | None = None,
    chrome_user_data: Path | None = None,
    chrome_profile_directory: str | None = None,
    debug_port: int | None = None,
) -> None:
    """레거시 호환 — 기본은 활성 슬롯 ChromeDebug."""
    slot_port, slot_ud = _slot_defaults()
    port = int(debug_port if debug_port is not None else slot_port)
    if chrome_user_data is None and profile_dir is None:
        open_chrome_debug(url, debug_port=port)
        return
    chrome = find_chrome_exe()
    if chrome is None:
        raise RuntimeError(
            "Google Chrome을 찾을 수 없습니다.\nChrome 설치 후 다시 시도하세요."
        )
    args: list[str] = [str(chrome)]
    if chrome_user_data is not None and chrome_profile_directory:
        args.append(f"--user-data-dir={chrome_user_data.resolve()}")
        args.append(f"--profile-directory={chrome_profile_directory}")
        args.append(f"--remote-debugging-port={port}")
    elif profile_dir is not None:
        profile_dir.mkdir(parents=True, exist_ok=True)
        args.append(f"--user-data-dir={profile_dir.resolve()}")
        args.append(f"--remote-debugging-port={port}")
    else:
        args.append(f"--remote-debugging-port={port}")
        args.append(f"--user-data-dir={slot_ud.resolve()}")
    args.append("--new-window")
    args.append(url)
    kwargs: dict = {"args": args, "close_fds": True}
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    subprocess.Popen(**kwargs)


def open_browser_for_account(
    url: str,
    *,
    email: str = "",
    fallback_profile_dir: Path | None = None,
    debug_port: int | None = None,
    restart_chrome: bool = False,
) -> dict[str, str]:
    """브라우저 열기 — 활성 슬롯 포트·프로필."""
    del email, fallback_profile_dir  # 호환용 인자
    ensure_chrome_slot()
    slot_port, _ud = _slot_defaults()
    port = int(debug_port if debug_port is not None else slot_port)
    return open_chrome_debug(
        url, debug_port=port, restart=restart_chrome
    )


def open_browser_for_manual_login(url: str, profile_dir: Path | None = None) -> None:
    del profile_dir
    open_chrome_debug(url)


def _is_followup_command(text: str) -> bool:
    """이어쓰기 칸에 넣는 짧은 씬 명령인지 (초기 붙여넣기 대용량과 구분)."""
    t = (text or "").strip()
    if not t:
        return False
    # SRT+프롬프트 대용량은 랜딩 New Image composer (CHARACTER LOOK 문구 포함해도)
    if len(t) > 5000:
        return False
    if t.startswith("===== CHARACTER LOOK"):
        return True
    if "CHARACTER LOOK" in t and "Chinese wuxia manhua" in t:
        return True
    return len(t) < 500 and t.upper().startswith("SRT_")


# 씬 직전 문맥(초). DRAW 구간과 분리해 넣음.
CONTEXT_LOOKBACK_SEC = 60

def _reference_image_block(ref_label: str) -> str:
    label = (ref_label or "reference").strip()
    return (
        f"===== REFERENCE IMAGE (attached: {label}) — GUIDE ONLY =====\n"
        "The attached thumbnail is for FACE/IDENTITY reference ONLY.\n"
        "Generate a brand-new FULL-RESOLUTION 16:9 illustration from scratch.\n"
        "Do NOT output the attachment, an upscaled attachment, edit-pass, or near-copy.\n"
        "Do NOT blur, smear, posterize, or reuse low-res pixels from the attachment.\n"
        "Use the attachment only to keep each character's face, hair style, and "
        "base outfit colors consistent.\n"
        "Do NOT copy composition, camera angle, background, props, lighting, or pose "
        "from the attachment.\n"
        "Draw pose, action, expression, environment, and framing strictly from "
        "SRT and SCENE PROMPT below.\n"
        "Sharp clean linework and full detail — ultra high-res digital manhua art.\n"
        "Do NOT redesign faces or merge into one generic pretty-boy look."
    )


_NO_SPEECH_BUBBLE_BLOCK = (
    "===== FORBIDDEN IN IMAGE (strict) =====\n"
    "Absolutely NO speech bubbles, comic balloons, dialogue balloons, thought bubbles, "
    "tooltips, help balloons, UI tips, callout boxes, caption boxes, chat bubbles, "
    "or any tailed balloon / dialogue box of any kind. "
    "NO written text, letters, Hangul, hanzi, kana, or Latin script on the image. "
    "If characters speak, show only facial expression, mouth shape, and body language — "
    "never draw a bubble, caption, or on-image dialogue."
)

_TITLE_CARD_FORBIDDEN_BLOCK = (
    "===== FORBIDDEN IN IMAGE (title card) =====\n"
    "NO speech bubbles, comic balloons, tooltips, logos, subtitles, or extra captions. "
    "NO characters, faces, figures, weapons, or story scene objects. "
    "ONLY the single centered chapter title line is allowed as on-image text."
)


def _title_card_scene_block(chapter_title: str) -> str:
    title = (chapter_title or "").strip()
    return (
        f"===== CHAPTER TITLE CARD (SRT_000) =====\n"
        f"Chinese wuxia manhua episode opening title card, 16:9 ultrawide, "
        f"dark elegant ink-wash gradient background with subtle mist and faint "
        f"paper texture, cinematic vignette.\n"
        f"Center of image: one large traditional brush-calligraphy title line, "
        f"exactly this text (same script, spacing, and punctuation):\n"
        f"「{title}」\n"
        f"Title must be clearly readable, vertically or horizontally centered, "
        f"high contrast against background. No other on-image text."
    )


def _title_card_instruction(label: str) -> str:
    return (
        f"{label} chapter title card only — generate now. "
        f"Show ONLY the centered chapter title on atmospheric background; "
        f"no figures, no story illustration. "
        f"After the image appears in this reply, output exactly one line only: "
        f"「{label} 이미지가 성공적으로 생성되었습니다.」 "
        f"Do not write success text without the image. "
        f"If generation fails, short error only. "
        f"No summary, caption, analysis, tips, table, or other prose."
    )


def build_generate_command(
    srt_sec: int,
    *,
    scene_prompt: str | None = None,
    srt_dialogue: str | None = None,
    srt_context: str | None = None,
    interval_sec: int = 20,
    context_lookback_sec: int = CONTEXT_LOOKBACK_SEC,
    character_look: str | None = None,
    png_dir: Path | str | None = None,
    state_tracker: Any | None = None,
    prompt_path: Path | str | None = None,
    reference_attached: bool = False,
    reference_label: str = "",
    chapter_title: str | None = None,
    dialogue_end_sec: int | None = None,
) -> str:
    """입력창 명령: 문맥(+실장면) + CHARACTER LOOK + 현재 구간 대본 + 생성 지시.

    Face Identity·LOOK 을 매 장 재삽입. 상태 추적은 character_consistency 플래그.
    직전 ``context_lookback_sec`` 대본은 CONTEXT(그리지 말 것)로만 넣고,
    이미지 사건은 ``[T, T+interval)`` SRT(+ SCENE PROMPT)만 그린다.

    ``SRT_000`` + ``chapter_title`` 이면 장 제목 카드(중앙 글씨) 전용 명령.
    """
    from scene_image.character_consistency import (
        CharacterStateTracker,
        build_character_look_for_scene,
        style_tail_for_scene,
    )
    from scene_image.scene_parse import is_real_scene_prompt

    n = max(0, int(srt_sec))
    label = f"SRT_{n:03d}"
    gap = max(1, int(interval_sec))
    end_sec = int(dialogue_end_sec) if dialogue_end_sec is not None else n + gap
    lookback = max(1, int(context_lookback_sec))
    dialogue = (srt_dialogue or "").strip()
    context = (srt_context or "").strip()
    real = scene_prompt if is_real_scene_prompt(scene_prompt) else None

    title_text = (chapter_title or "").strip()
    if n == 0 and title_text:
        parts = [
            _title_card_scene_block(title_text),
            f"===== STYLE =====\n{style_tail_for_scene(has_characters=False)}",
            _TITLE_CARD_FORBIDDEN_BLOCK,
            _title_card_instruction(label),
        ]
        return "\n\n".join(parts)

    # LOOK·인물 탐지·상처 판정은 현재 구간(+실프롬프트)만 — CONTEXT 미포함
    look = (character_look or "").strip()
    tr = state_tracker
    if not look:
        look, tr = build_character_look_for_scene(
            n,
            dialogue=dialogue,
            scene_prompt=real or scene_prompt,
            png_dir=png_dir,
            tracker=tr if isinstance(tr, CharacterStateTracker) else None,
            prompt_path=prompt_path,
        )
    elif isinstance(tr, CharacterStateTracker) and png_dir:
        tr.save(png_dir)

    parts: list[str] = []
    if look:
        parts.append(f"===== CHARACTER LOOK ({label}) =====\n{look}")
    if reference_attached:
        parts.append(_reference_image_block(reference_label or label))
    if real:
        parts.append(f"===== SCENE PROMPT ({label}) =====\n{real}")
    if context and n > 0:
        c0 = max(0, n - lookback)
        parts.append(
            f"===== CONTEXT [{c0}, {n}) — background only; do NOT draw =====\n"
            f"{context}\n"
            f"(Prior ~{lookback}s of dialogue for place, who, and cause only. "
            f"Do NOT illustrate events from CONTEXT. "
            f"Draw ONLY the SRT [{n}, {end_sec}) block below"
            + (" and SCENE PROMPT" if real else "")
            + ".)"
        )
    if dialogue:
        parts.append(
            f"===== SRT [{n}, {end_sec}) — DRAW THIS SCENE ONLY =====\n{dialogue}"
        )
    if not real:
        has_chars = bool(look)
        parts.append(f"===== STYLE =====\n{style_tail_for_scene(has_characters=has_chars)}")
    # SCENE PROMPT 가 있어도 말풍선 금지는 매번 명시 (스타일 블록 생략 시에도)
    parts.append(_NO_SPEECH_BUBBLE_BLOCK)
    tr_obj = tr if isinstance(tr, CharacterStateTracker) else None
    multi = False
    if tr_obj and look:
        present = tr_obj.detect_present(dialogue, real or scene_prompt)
        multi = len(present) >= 2
    instr = (
        tr_obj.build_scene_instruction(label, multi_character=multi)
        if tr_obj
        else (
            f"{label} Chinese wuxia manhua illustration — generate now. "
            f"Keep character face and outfit consistent with Character Bible. "
            f"Absolutely no speech bubbles, comic balloons, tooltips, callouts, "
            f"or any on-image text. "
            f"After the image appears in this reply, output exactly one line only: "
            f"「{label} 이미지가 성공적으로 생성되었습니다.」 "
            f"Do not write success text without the image. "
            f"If generation fails, short error only. "
            f"No summary, caption, analysis, tips, table, or other prose."
        )
    )
    if context and n > 0:
        instr += (
            f" Ignore CONTEXT for what appears in the image; "
            f"show only what belongs to SRT [{n}, {end_sec})."
        )
    if reference_attached:
        instr += (
            " Reference attachment is guide-only: produce a fresh high-resolution "
            "render from the prompt — not an upscale, repost, or pixel-copy of "
            "the attached image."
        )
    parts.append(instr)
    return "\n\n".join(parts)


def build_generate_command_from_sources(
    srt_sec: int,
    *,
    scene_prompt: str | None = None,
    srt_path: str | Path | None = None,
    interval_sec: int = 20,
    context_lookback_sec: int = CONTEXT_LOOKBACK_SEC,
    prompt_path: str | Path | None = None,
    png_dir: Path | str | None = None,
    state_tracker: Any | None = None,
    reference_attached: bool = False,
    reference_label: str = "",
    next_scene_sec: int | None = None,
) -> str:
    """SRT 파일·장면 프롬프트에서 매 장 전송 문구를 만든다."""
    from scene_image.scene_parse import (
        detect_chapter_title_from_srt,
        is_real_scene_prompt,
        srt_context_before,
        srt_dialogue_for_window,
        srt_dialogue_until_next_scene,
    )

    if next_scene_sec is not None:
        dialogue = srt_dialogue_until_next_scene(
            srt_path, srt_sec, next_scene_sec
        )
        dialogue_end = int(next_scene_sec)
    else:
        dialogue = srt_dialogue_for_window(srt_path, srt_sec, interval_sec)
        dialogue_end = None
    context = srt_context_before(
        srt_path, srt_sec, lookback_sec=context_lookback_sec
    )
    real = scene_prompt if is_real_scene_prompt(scene_prompt) else None
    chapter_title = (
        detect_chapter_title_from_srt(srt_path) if int(srt_sec) == 0 else None
    )
    return build_generate_command(
        srt_sec,
        scene_prompt=real,
        srt_dialogue=dialogue or None,
        srt_context=context or None,
        interval_sec=interval_sec,
        context_lookback_sec=context_lookback_sec,
        png_dir=png_dir,
        state_tracker=state_tracker,
        prompt_path=prompt_path,
        reference_attached=reference_attached,
        reference_label=reference_label,
        chapter_title=chapter_title,
        dialogue_end_sec=dialogue_end,
    )


def build_prompt_with_filename(prompt: str, srt_sec: int) -> str:
    """생성 요청 명령어 — 실장면 프롬프트가 있으면 함께 넣음."""
    return build_generate_command(srt_sec, scene_prompt=prompt)


def has_playwright() -> bool:
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False
    return True


def preferred_genspark_url(user_url: str = "") -> str:
    u = (user_url or "").strip()
    return u or GENSPARK_AI_IMAGE_URL


def _maybe_set_work_url(current: str, page_url: str) -> str:
    """더 좋은 대화 URL이면 갱신."""
    pu = (page_url or "").strip()
    if not pu or _is_wrong_agent_url(pu):
        return current
    if _score_ai_image_url(pu) >= 35 and _score_ai_image_url(pu) >= _score_ai_image_url(
        current or ""
    ):
        return pu
    return current or pu


async def _is_logged_in(page: Any) -> bool:
    """이미 로그인된 세션인지 휴리스틱 판별."""
    try:
        url = (page.url or "").lower()
        if "accounts.google.com" in url:
            return False
        return bool(
            await page.evaluate(
                """() => {
                  const t = ((document.body && document.body.innerText) || '').slice(0, 8000);
                  if (/accounts\\.google\\.com/i.test(location.href)) return false;
                  const needsLogin = /Sign\\s*in|Log\\s*in|로그인|Continue with Google/i.test(t)
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


async def _type_into(page: Any, selectors: tuple[str, ...], text: str) -> bool:
    """입력란 클릭 후 키보드로 입력 (fill보다 Google 폼에 안정적)."""
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


async def _click_login_entry(page: Any) -> bool:
    # 단독 "Google" 은 업로드/기타 UI까지 잡혀 파일창이 뜰 수 있어 제외
    for text in (
        "Continue with Google",
        "Google로 계속",
        "Sign in with Google",
        "Sign in with google",
        "Sign in",
        "Log in",
        "Login",
        "로그인",
    ):
        if await _click_by_text(page, (text,)):
            return True
    return False


async def _pick_google_account(page: Any, email: str) -> bool:
    """계정 선택 화면에서 해당 이메일 클릭."""
    email = (email or "").strip()
    if not email:
        return False
    # data-identifier / 이메일 텍스트
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
    """accounts.google.com (또는 팝업)에서 이메일·비밀번호 자동 입력."""
    email = (email or "").strip()
    password = password or ""
    if not email or not password:
        return False

    # 계정 선택 목록이 있으면 클릭
    await _pick_google_account(page, email)

    # 이메일 단계
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
        await page.wait_for_timeout(1800)

    # 다시 계정 선택일 수 있음
    await _pick_google_account(page, email)
    await page.wait_for_timeout(600)

    # 비밀번호 단계 (최대 ~20초 대기)
    pw_ok = False
    for _ in range(20):
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
        # "Use another account" 후 이메일 재입력
        if await _type_into(
            page,
            ('input[type="email"]', "#identifierId", 'input[name="identifier"]'),
            email,
        ):
            await _click_next(page)
        await page.wait_for_timeout(800)

    if not pw_ok:
        return False

    await _click_next(page)
    await page.wait_for_timeout(2000)

    # 추가 확인 화면
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
                await page.wait_for_timeout(700)
        except Exception:
            pass
    return True


async def _wait_back_to_genspark(page: Any, *, seconds: int = 45) -> bool:
    for _ in range(max(1, seconds * 2)):
        url = (page.url or "").lower()
        if "genspark.ai" in url and "accounts.google.com" not in url:
            await page.wait_for_timeout(1000)
            return True
        await page.wait_for_timeout(500)
    return "genspark.ai" in (page.url or "").lower()


async def _google_login(
    page: Any,
    email: str,
    password: str,
    *,
    context: Any | None = None,
) -> bool:
    """Google 계정으로 Genspark 로그인 — 이메일/비밀번호 자동 입력."""
    email = (email or "").strip()
    password = password or ""
    if not email or not password:
        return False

    login_page = page

    # Google 로그인 팝업 또는 리다이렉트 대기
    for attempt in range(3):
        popup: Any | None = None
        if context is not None:
            try:
                async with context.expect_page(timeout=4000) as pi:
                    await _click_login_entry(page)
                popup = await pi.value
            except Exception:
                await _click_login_entry(page)
        else:
            await _click_login_entry(page)

        if popup is not None:
            try:
                await popup.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:
                pass
            login_page = popup
            break

        # 같은 탭에서 Google로 이동했는지
        for _ in range(20):
            if "accounts.google.com" in (page.url or "").lower():
                login_page = page
                break
            await _click_by_text(
                page,
                (
                    "Continue with Google",
                    "Google로 계속",
                    "Sign in with Google",
                    "Google",
                ),
            )
            await page.wait_for_timeout(400)
        else:
            if attempt < 2:
                continue
        break

    # 로그인 페이지에서 자격 증명 입력
    for _ in range(30):
        cur = (login_page.url or "").lower()
        if "accounts.google.com" in cur or await login_page.locator(
            'input[type="email"], input[type="password"], #identifierId'
        ).count():
            break
        await page.wait_for_timeout(400)
        # context의 다른 페이지에 Google 로그인일 수 있음
        if context is not None:
            for p in context.pages:
                if "accounts.google.com" in (p.url or "").lower():
                    login_page = p
                    break

    filled = await _fill_google_credentials(login_page, email, password)
    if not filled:
        # Genspark 자체 이메일/비밀번호 폼
        e_ok = await _type_into(
            page,
            (
                'input[type="email"]',
                'input[name="email"]',
                'input[autocomplete="username"]',
            ),
            email,
        )
        p_ok = await _type_into(
            page,
            (
                'input[type="password"]',
                'input[name="password"]',
                'input[autocomplete="current-password"]',
            ),
            password,
        )
        if e_ok and p_ok:
            await _click_next(page)
            filled = True

    if not filled:
        return False

    # 팝업이 닫히면 원래 페이지로
    if login_page is not page:
        try:
            await login_page.wait_for_event("close", timeout=20000)
        except Exception:
            pass
        try:
            if not login_page.is_closed():
                await _wait_back_to_genspark(login_page, seconds=20)
        except Exception:
            pass

    ok = await _wait_back_to_genspark(page, seconds=40)
    if ok:
        return True
    return await _is_logged_in(page)


async def _ensure_login(
    page: Any,
    email: str,
    password: str,
    *,
    context: Any | None = None,
    force: bool = False,
) -> dict[str, bool]:
    """로그인 필요 시 Google/폼에 계정·비밀번호 자동 입력."""
    if not force and await _is_logged_in(page):
        return {"logged_in": True, "attempted": False, "filled": False}
    if not (email or "").strip() or not password:
        return {"logged_in": False, "attempted": False, "filled": False}

    # Sign in이 보이면 무조건 자동 입력 시도
    needs = True
    try:
        t = await page.evaluate(
            "() => ((document.body && document.body.innerText) || '').slice(0, 5000)"
        )
        needs = bool(
            re.search(r"Sign\s*in|Log\s*in|로그인|Continue with Google", t or "", re.I)
        ) or not await _is_logged_in(page)
    except Exception:
        needs = True

    if not needs and not force:
        return {"logged_in": True, "attempted": False, "filled": False}

    ok = await _google_login(page, email, password, context=context)
    return {
        "logged_in": bool(ok or await _is_logged_in(page)),
        "attempted": True,
        "filled": True,
    }


async def _toolbar_model_label(page: Any) -> str:
    """하단 툴바의 현재 모델 칩 텍스트 (예: Nano Banana 2 Flash)."""
    try:
        return str(
            await page.evaluate(
                """() => {
                  const vh = window.innerHeight || 800;
                  const hits = [];
                  const els = document.querySelectorAll(
                    'button, [role="button"], [role="combobox"], [aria-haspopup], div, span'
                  );
                  for (const el of els) {
                    const r = el.getBoundingClientRect();
                    if (r.bottom < vh * 0.58 || r.top > vh - 6) continue;
                    if (r.width < 24 || r.height < 12 || r.height > 56) continue;
                    const t = (el.innerText || el.textContent || '')
                      .replace(/\\s+/g, ' ').trim();
                    if (!t || t.length > 70) continue;
                    if (!/banana|🍌|flash/i.test(t)) continue;
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


def _label_is_nano_banana_pro(text: str) -> bool:
    t = (text or "").replace("\n", " ")
    if re.search(r"flash", t, re.I):
        return False
    return bool(re.search(r"nano\s*banana\s*(2\s*)?pro|banana\s*pro", t, re.I))


async def _page_has_nano_banana(page: Any) -> bool:
    """툴바에 Nano Banana Pro 가 선택돼 있는지 (Flash 제외)."""
    return _label_is_nano_banana_pro(await _toolbar_model_label(page))


async def _open_model_picker(page: Any) -> str:
    """하단 모델 칩(Flash/Banana)을 눌러 목록을 연다. 클릭한 라벨 반환."""
    try:
        return str(
            await page.evaluate(
                """() => {
                  const vh = window.innerHeight || 800;
                  const hits = [];
                  const els = document.querySelectorAll(
                    'button, [role="button"], [role="combobox"], [aria-haspopup], div, span'
                  );
                  for (const el of els) {
                    const r = el.getBoundingClientRect();
                    if (r.bottom < vh * 0.58 || r.top > vh - 6) continue;
                    if (r.width < 24 || r.height < 12 || r.height > 56) continue;
                    const t = (el.innerText || el.textContent || '')
                      .replace(/\\s+/g, ' ').trim();
                    if (!t || t.length > 70) continue;
                    if (!/nano\\s*banana|🍌|flash/i.test(t)) continue;
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


async def _click_nano_banana_pro_option(page: Any) -> str:
    """팝업 목록에서만 Flash 가 아닌 Pro 항목을 클릭 (본문 텍스트 클릭 금지)."""
    try:
        return str(
            await page.evaluate(
                """() => {
                  const roots = Array.from(document.querySelectorAll(
                    '[role="listbox"], [role="menu"], [role="dialog"], '
                    + '[data-radix-popper-content-wrapper], [class*="popover" i], '
                    + '[class*="dropdown" i], [class*="Menu" i]'
                  ));
                  const scope = roots.length ? roots : [];
                  const nodes = [];
                  for (const root of scope) {
                    nodes.push(...root.querySelectorAll(
                      '[role="option"], [role="menuitem"], button, li, div, span'
                    ));
                  }
                  const hits = [];
                  for (const el of nodes) {
                    const t = (el.innerText || el.textContent || '')
                      .replace(/\\s+/g, ' ').trim();
                    if (!t || t.length > 64) continue;
                    if (/flash/i.test(t)) continue;
                    if (!/nano\\s*banana\\s*(2\\s*)?pro|banana\\s*pro/i.test(t))
                      continue;
                    const r = el.getBoundingClientRect();
                    if (r.width < 20 || r.height < 10 || r.height > 80) continue;
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


async def _select_nano_banana_pro(
    page: Any,
    *,
    custom_selector: str = "",
    model_texts: tuple[str, ...] | None = None,
) -> bool:
    """하단 모델 칩을 열어 Nano Banana Pro 를 선택 (Flash 가 기본이면 교체)."""
    del model_texts  # 칩·옵션 텍스트로 직접 판별
    before = await _toolbar_model_label(page)
    if _label_is_nano_banana_pro(before):
        _tab_log(f"모델 이미 Pro: {before[:60]}")
        return True
    _tab_log(f"모델 선택 시작 현재={before[:60] or '(없음)'}")

    if custom_selector.strip():
        loc = page.locator(custom_selector.strip()).first
        try:
            if await loc.is_visible(timeout=1500):
                await loc.click(timeout=4000)
                await page.wait_for_timeout(400)
        except Exception:
            pass

    opened = await _open_model_picker(page)
    if opened:
        _tab_log(f"모델 칩 클릭: {opened[:60]}")
        await page.wait_for_timeout(800)
    else:
        # 폴백: Model/모델 버튼
        for sel in (
            "button:has-text('Model')",
            "button:has-text('모델')",
            "[aria-label*='Model' i]",
            "[aria-label*='모델' i]",
            "[role='combobox']",
        ):
            loc = page.locator(sel).first
            try:
                if not await loc.is_visible(timeout=600):
                    continue
                await loc.click(timeout=3000)
                await page.wait_for_timeout(800)
                opened = "fallback"
                break
            except Exception:
                continue

    picked = await _click_nano_banana_pro_option(page)
    if not picked:
        try:
            loc = page.get_by_role(
                "option", name=re.compile(r"Nano Banana Pro", re.I)
            ).first
            if await loc.is_visible(timeout=1200):
                label = (await loc.inner_text() or "").strip()
                if not re.search(r"flash", label, re.I):
                    await loc.click(timeout=4000)
                    picked = label or "Nano Banana Pro"
                    _tab_log(f"Pro role=option 클릭: {picked[:60]}")
                    await page.wait_for_timeout(700)
        except Exception:
            pass
    if picked:
        _tab_log(f"Pro 옵션 클릭: {picked[:60]}")
        await page.wait_for_timeout(700)
    else:
        for text in ("Nano Banana Pro", "Nano banana pro", "Banana Pro"):
            loc = page.locator(
                f"[role='option']:has-text('{text}'), "
                f"[role='menuitem']:has-text('{text}')"
            ).first
            try:
                if not await loc.is_visible(timeout=800):
                    continue
                label = (await loc.inner_text() or "").strip()
                if re.search(r"flash", label, re.I):
                    continue
                await loc.click(timeout=4000)
                _tab_log(f"Pro 옵션 폴백 클릭: {label[:60]}")
                await page.wait_for_timeout(700)
                picked = label
                break
            except Exception:
                continue

    after = await _toolbar_model_label(page)
    ok = _label_is_nano_banana_pro(after)
    if not ok and picked and not re.search(r"flash", after or "", re.I):
        # 칩을 못 읽어도 목록에서 Pro 를 눌렀으면 성공으로 봄
        ok = True
    _tab_log(f"모델 선택 결과 ok={ok} 칩={after[:60] or '(없음)'} picked={picked[:40]}")
    return ok


async def _is_file_upload_target(loc: Any) -> bool:
    """파일 선택(Windows 파일 찾기)을 여는 요소인지."""
    try:
        return bool(
            await loc.evaluate(
                """el => {
                  if (!el) return true;
                  const t = (el.tagName || '').toLowerCase();
                  const typ = ((el.getAttribute && el.getAttribute('type')) || '').toLowerCase();
                  if (t === 'input' && typ === 'file') return true;
                  if (el.closest && el.closest('input[type="file"]')) return true;
                  if (el.querySelector && el.querySelector('input[type="file"]')) return true;
                  const al = (
                    (el.getAttribute('aria-label') || '') + ' ' +
                    (el.getAttribute('title') || '') + ' ' +
                    (el.textContent || '')
                  ).toLowerCase();
                  if (/upload image|upload file|choose file|browse file|첨부|파일 선택|파일 업로드|이미지 업로드/.test(al))
                    return true;
                  return false;
                }"""
            )
        )
    except Exception:
        return True


async def _prompt_editor_candidates(
    page: Any, *, prefer_followup: bool = False
) -> list[Any]:
    """프롬프트 입력 후보.

    ``prefer_followup=True`` 이면 결과 페이지 **이어쓰기** 칸만 우선하고,
    New Image / 검색 칸은 제외한다.
    agents 채팅 입력은 왼쪽 패널(aside)에 있어도 유지한다.
    """
    scored: list[tuple[float, float, float, Any]] = []
    for sel in (
        "textarea:visible",
        "[contenteditable='true']:visible",
        "[role='textbox']:visible",
        "div[contenteditable='true']:visible",
        "input[type='text']:visible",
    ):
        loc = page.locator(sel)
        try:
            n = await loc.count()
        except Exception:
            continue
        for i in range(min(n, 20)):
            item = loc.nth(i)
            try:
                if not await item.is_visible():
                    continue
                if await _is_file_upload_target(item):
                    continue
                box = await item.bounding_box()
                if not box:
                    continue
                if box.get("width", 0) < 100 or box.get("height", 0) < 16:
                    continue
                meta = await item.evaluate(
                    """el => {
                      if (!el) return {kind:'reject', ro:true, ph:''};
                      const ro = !!(el.disabled || el.readOnly
                        || el.getAttribute('aria-readonly') === 'true'
                        || el.getAttribute('contenteditable') === 'false');
                      const ph = (
                        (el.getAttribute('placeholder') || '') + ' ' +
                        (el.getAttribute('aria-label') || '') + ' ' +
                        (el.getAttribute('data-placeholder') || '') + ' ' +
                        (el.getAttribute('title') || '')
                      ).toLowerCase();
                      const inSearch = !!(el.closest && el.closest(
                        '[role="search"]'
                      ));
                      const inNavOnly = !!(el.closest && el.closest(
                        'nav, [role="navigation"], header'
                      )) && !(el.closest('aside') || el.closest('[class*="sidebar" i]')
                        || el.closest('[class*="chat" i]') || el.closest('[class*="composer" i]')
                        || el.closest('[class*="prompt" i]') || el.closest('main'));
                      let kind = 'ok';
                      if (ro) kind = 'reject';
                      else if (/^\\s*(search|검색|filter|온라인에서)/.test(ph) || inSearch) kind = 'reject';
                      else if (/new\\s*image|create\\s*(an?\\s*)?image|새\\s*이미지|새\\s*대화|new\\s*chat|start\\s*a\\s*new/.test(ph))
                        kind = 'new';
                      // Genspark agents: "상상하는 장면을 설명해 주세요"
                      else if (/follow|ask|message|reply|계속|이어|메시지|질문|prompt|describe|설명|상상|장면|tell\\s*me|type\\s*(a\\s*)?(message|prompt)|chat|send\\s*a\\s*message|장면을\\s*설명/.test(ph))
                        kind = 'follow';
                      else if (inNavOnly) kind = 'reject';
                      return {kind, ro, ph, inSearch};
                    }"""
                )
                kind = str((meta or {}).get("kind") or "ok")
                if kind == "reject" or (meta or {}).get("ro"):
                    continue
                if prefer_followup and kind == "new":
                    continue
                # follow 가산, new 감점, 하단·넓은 칸 우선
                kind_boost = 50.0 if kind == "follow" else (0.0 if kind == "ok" else -30.0)
                if prefer_followup and kind != "follow":
                    kind_boost -= 20.0
                y = float(box.get("y", 0)) + float(box.get("height", 0))
                area = float(box.get("width", 0)) * float(box.get("height", 0))
                # agents 대화: 왼쪽 하단 채팅 입력 = 이어쓰기
                if prefer_followup and kind in ("ok", "follow"):
                    try:
                        vh = float(
                            await page.evaluate("() => window.innerHeight || 800")
                        )
                    except Exception:
                        vh = 800.0
                    if y >= vh * 0.35 and area >= 4000:
                        kind_boost = max(kind_boost, 55.0)
                    # placeholder 한국어 장면 설명
                    ph = str((meta or {}).get("ph") or "")
                    if re.search(r"상상|장면|설명|message|ask", ph, re.I):
                        kind_boost = max(kind_boost, 60.0)
                scored.append((kind_boost, y, area, item))
            except Exception:
                continue
    scored.sort(key=lambda t: (t[0], t[1], t[2]), reverse=True)
    if prefer_followup:
        follows = [t for t in scored if t[0] >= 45.0]
        if follows:
            scored = follows
        elif scored:
            # follow 미표기여도 최상단 후보 사용 (aside 채팅칸)
            scored = scored[:3]
    out: list[Any] = []
    seen: set[int] = set()
    for _kb, _y, _a, item in scored:
        try:
            key = await item.evaluate(
                "el => el.outerHTML.length + '|' + (el.className||'')"
            )
            hk = hash(key)
        except Exception:
            hk = id(item)
        if hk in seen:
            continue
        seen.add(hk)
        out.append(item)
    if prefer_followup and not out:
        # 최후: placeholder 로 직접 찾기
        for pat in (
            r"상상하는\s*장면",
            r"장면을\s*설명",
            r"Describe",
            r"Ask\s*(me|anything)?",
            r"Message",
            r"메시지를",
        ):
            try:
                loc = page.get_by_placeholder(re.compile(pat, re.I))
                n = await loc.count()
                for i in range(min(n, 4)):
                    item = loc.nth(i)
                    if await item.is_visible():
                        out.append(item)
            except Exception:
                continue
    if not out:
        # 랜딩/에이전트 공통 최후 수단: 보이는 textarea·textbox 전부
        raw = await _raw_visible_editors(page)
        out.extend(raw)
    return out


async def _raw_visible_editors(page: Any) -> list[Any]:
    """필터 없이 보이는 편집 가능 입력란 (진단·최후 폴백)."""
    out: list[Any] = []
    try:
        infos = await page.evaluate(
            """() => {
              const nodes = Array.from(document.querySelectorAll(
                'textarea, [contenteditable="true"], [role="textbox"]'
              ));
              return nodes.map((el) => {
                const r = el.getBoundingClientRect();
                const st = window.getComputedStyle(el);
                const ph = (el.getAttribute('placeholder') || '')
                  + ' ' + (el.getAttribute('aria-label') || '');
                const vis = st.display !== 'none' && st.visibility !== 'hidden'
                  && Number(st.opacity || '1') > 0.1
                  && r.width >= 80 && r.height >= 14
                  && r.bottom > 0 && r.top < window.innerHeight;
                const ro = !!(el.disabled || el.readOnly
                  || el.getAttribute('aria-readonly') === 'true');
                return {
                  vis, ro, tag: (el.tagName || '').toLowerCase(),
                  ph: ph.slice(0, 100),
                  w: Math.round(r.width), h: Math.round(r.height),
                  y: Math.round(r.bottom),
                  area: Math.round(r.width * r.height)
                };
              });
            }"""
        )
    except Exception as ex:
        _tab_log(f"raw editors evaluate 실패: {ex}")
        infos = []
    _tab_log(
        "raw editors: "
        + (
            " | ".join(
                f"{i.get('tag')} vis={i.get('vis')} ro={i.get('ro')} "
                f"{i.get('w')}x{i.get('h')} ph={i.get('ph')!r}"
                for i in (infos or [])[:12]
            )
            or "(none)"
        )
    )
    # Playwright: 큰 textarea / contenteditable 우선
    for sel in ("textarea", "[contenteditable='true']", "[role='textbox']"):
        try:
            loc = page.locator(sel)
            n = await loc.count()
        except Exception:
            continue
        scored: list[tuple[float, Any]] = []
        for i in range(min(n, 16)):
            el = loc.nth(i)
            try:
                if not await el.is_visible(timeout=800):
                    continue
                box = await el.bounding_box()
                if not box or box["width"] < 80 or box["height"] < 14:
                    continue
                ph = (
                    (await el.get_attribute("placeholder") or "")
                    + " "
                    + (await el.get_attribute("aria-label") or "")
                )
                if re.search(r"온라인에서|^\s*search|^\s*검색", ph, re.I):
                    continue
                # disabled?
                try:
                    if await el.is_disabled():
                        continue
                except Exception:
                    pass
                area = float(box["width"]) * float(box["height"])
                scored.append((area, el))
            except Exception:
                continue
        scored.sort(key=lambda t: t[0], reverse=True)
        for area, el in scored[:3]:
            out.append(el)
            _tab_log(f"raw pick {sel} area={area:.0f}")
        if out:
            break
    return out


def _copy_text_to_clipboard_win(text: str) -> bool:
    """Windows 클립보드에 텍스트 복사 (이미지 첨부 후 명령 입력용)."""
    if sys.platform != "win32":
        return False
    if not (text or "").strip():
        return False
    tmp = Path(os.environ.get("TEMP", ".")) / "_genspark_clip.txt"
    try:
        tmp.write_text(text, encoding="utf-8")
        path_esc = str(tmp.resolve()).replace("'", "''")
        r = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                f"Get-Content -LiteralPath '{path_esc}' -Raw -Encoding UTF8"
                " | Set-Clipboard",
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
            creationflags=_subprocess_no_window_flags(),
        )
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


async def _composer_contains_text(page: Any, needle: str) -> bool:
    """composer 본문에 ``needle`` 포함 여부."""
    n = (needle or "").strip()
    if len(n) < 4:
        return False
    try:
        return bool(
            await page.evaluate(
                """(needle) => {
                  const els = document.querySelectorAll(
                    "textarea,[contenteditable='true'],[role='textbox']"
                  );
                  for (const el of els) {
                    const t = el.value || el.innerText || el.textContent || '';
                    if (t.includes(needle)) return true;
                  }
                  return false;
                }""",
                n[:120],
            )
        )
    except Exception:
        return False


def _fill_verify_needle(text: str) -> str:
    """fill 성공 확인용 짧은 문자열."""
    m = _SRT_LABEL_RE.search(text or "")
    if m:
        return f"SRT_{int(m.group(1)):03d}"
    t = (text or "").strip()
    return t[:60] if t else ""


async def _append_to_first_editable(page: Any, text: str) -> bool:
    """같은 입력창 끝에 텍스트 추가 (기존 SRT·프롬프트 유지)."""
    for item in await _prompt_editor_candidates(page, prefer_followup=False):
        try:
            await item.click(timeout=3000)
            await page.keyboard.press("Control+End")
            await page.keyboard.insert_text("\n\n" + text)
            await page.wait_for_timeout(300)
            return True
        except Exception:
            continue
    return False


async def _fill_first_editable(
    page: Any,
    text: str,
    *,
    prefer_followup: bool | None = None,
    skip_ready_wait: bool = False,
    ready_timeout_sec: float = 12.0,
    after_image_attach: bool = False,
) -> bool:
    """입력창에 텍스트 넣기.

    짧은 이어쓰기 명령(``SRT_XXX …``)은 결과 페이지 이어쓰기 칸만 사용.
    ``skip_ready_wait=True`` 이면 입력란 대기(중복)를 생략한다.
    ``after_image_attach=True`` — Ctrl+V 이미지 첨부 직후: append·insert_text만
    (OS 클립보드 이미지가 남아 Control+V 텍스트 붙여넣기가 막히는 것 방지).
    """
    if prefer_followup is None:
        prefer_followup = _is_followup_command(text)
    # agents?id= 대화면 이어쓰기 강제
    if _score_ai_image_url(page.url or "") >= 40:
        prefer_followup = True
    if not skip_ready_wait:
        await _wait_editor_ready(page, timeout_sec=ready_timeout_sec)
    try:
        await page.evaluate(
            """() => {
              const h = document.body && document.body.scrollHeight || 0;
              window.scrollTo(0, Math.max(0, h));
            }"""
        )
        await page.wait_for_timeout(120)
    except Exception:
        pass
    candidates = await _prompt_editor_candidates(
        page, prefer_followup=prefer_followup
    )
    if prefer_followup and not candidates:
        # 이어쓰기 칸을 못 찾으면 New Image 칸으로 가지 않음 — 일반 후보만 재시도
        candidates = await _prompt_editor_candidates(page, prefer_followup=False)
    if not candidates:
        _tab_log(
            f"fill 실패: 입력칸 없음 follow={prefer_followup} "
            f"url={(page.url or '')[:100]}"
        )
        return False
    verify = _fill_verify_needle(text)
    for item in candidates:
        try:
            await item.scroll_into_view_if_needed(timeout=3000)
            await item.click(timeout=3000)
            if after_image_attach:
                _clear_clipboard_win()
                await page.keyboard.press("End")
                await page.keyboard.insert_text("\n\n" + text)
                await page.wait_for_timeout(200)
                if verify and await _composer_contains_text(page, verify):
                    _tab_log(
                        f"fill OK(append) after_attach follow={prefer_followup} "
                        f"chars={len(text)}"
                    )
                    return True
                _tab_log("fill: append after_attach 미확인 — fill 폴백")
            if len(text) > 400 and not after_image_attach:
                try:
                    if sys.platform == "win32":
                        _copy_text_to_clipboard_win(text)
                    else:
                        await page.evaluate(
                            """async (t) => {
                              await navigator.clipboard.writeText(t);
                            }""",
                            text,
                        )
                    await page.keyboard.press("Control+A")
                    await page.keyboard.press("Control+V")
                    await page.wait_for_timeout(220)
                    if not verify or await _composer_contains_text(
                        page, verify
                    ):
                        _tab_log(
                            f"fill OK(clip) follow={prefer_followup} "
                            f"chars={len(text)}"
                        )
                        return True
                    _tab_log("fill: clip 미확인 — fill 폴백")
                except Exception:
                    pass
            try:
                if after_image_attach:
                    raise RuntimeError("append-only skip fill")
                await item.fill(text, timeout=60_000)
            except Exception:
                if after_image_attach:
                    await item.click(timeout=2000)
                    await page.keyboard.press("End")
                else:
                    await page.keyboard.press("Control+A")
                await page.keyboard.insert_text(
                    text if not after_image_attach else "\n\n" + text
                )
            await page.wait_for_timeout(150)
            if verify and not await _composer_contains_text(page, verify):
                _tab_log(
                    f"fill 미확인 follow={prefer_followup} verify={verify}"
                )
                continue
            _tab_log(
                f"fill OK follow={prefer_followup} chars={len(text)} "
                f"url={(page.url or '')[:100]}"
            )
            return True
        except Exception:
            continue
    _tab_log(f"fill 실패: 후보 {len(candidates)}개")
    return False


async def _composer_text_len(page: Any) -> int:
    """이어쓰기 입력창 본문 길이 (전송 여부 확인용)."""
    try:
        return int(
            await page.evaluate(
                """() => {
                  const els = Array.from(document.querySelectorAll(
                    "textarea, [contenteditable='true'], [role='textbox']"
                  ));
                  let best = 0;
                  const vh = window.innerHeight || 800;
                  for (const el of els) {
                    const r = el.getBoundingClientRect();
                    if (r.width < 40 || r.height < 18) continue;
                    if (r.bottom < vh * 0.35) continue;
                    const t = (el.value || el.innerText || el.textContent || '').trim();
                    if (t.length > best) best = t.length;
                  }
                  return best;
                }"""
            )
        )
    except Exception:
        return 0


async def _submit_looks_sent(page: Any, *, before_len: int) -> bool:
    """입력창 본문이 줄어야 전송된 것으로 본다.

    이전 응답의 「백그라운드에서 진행」문구만으로 성공 처리하면
    명령이 입력창에 남은 채 멈춘다.
    """
    after = await _composer_text_len(page)
    if before_len >= 30 and after < max(12, before_len // 3):
        return True
    return False


async def _chat_shows_submission_started(page: Any) -> bool:
    """composer 아래 최신 구간 — 생성 시작·작품 생성 안내."""
    try:
        return bool(
            await page.evaluate(
                """() => {
                  const t = ((document.body && document.body.innerText) || '')
                    .slice(-2800);
                  return /작품\\s*생성\\s*작업을\\s*시작|작품\\s*생성[^\\n]{0,40}시작|
                    이미지\\s*생성[^\\n]{0,40}시작|generation\\s*has\\s*started|
                    started\\s*generating|creating\\s*your\\s*image/i.test(t);
                }"""
            )
        )
    except Exception:
        return False


async def _submit_sent_ok(
    page: Any,
    *,
    before_len: int,
    after_attach: bool = False,
    editor: Any | None = None,
    before_thumbs: int = 0,
) -> tuple[bool, str]:
    """전송 성공 여부 — 텍스트 감소·생성 시작·입력창 비움."""
    # 로그인 리다이렉트 등으로 genspark.ai를 벗어나면 입력창이 통째로 사라져
    # 텍스트 감소·입력창 비움이 전송 성공처럼 보인다.
    if "genspark.ai" not in (page.url or "").lower():
        _tab_log(f"submit 미확인 — genspark 밖 {_page_ctx_label(page.url or '')}")
        return False, "off-site"
    after = await _composer_text_len(page)
    generating = await _is_generating(page)
    pending = await _chat_has_pending_image_generation(page)
    if await _submit_looks_sent(page, before_len=before_len):
        return True, "composer-shrink"
    if generating:
        return True, "generating"
    if pending:
        return True, "pending-card"
    if after_attach and await _chat_shows_submission_started(page):
        if generating or pending:
            return True, "submission-started"
    if after_attach and before_len >= 30 and after <= 8:
        return True, "composer-empty"
    if after_attach and editor is not None and before_thumbs > 0:
        after_thumbs = await _composer_pending_thumb_count(page, editor)
        if after_thumbs == 0 and (
            generating
            or pending
            or await _chat_shows_submission_started(page)
            or after < max(12, before_len // 3)
        ):
            return True, "attach-cleared"
    return False, ""


async def _composer_box(page: Any) -> dict[str, float] | None:
    try:
        box = await page.evaluate(
            """() => {
              const vh = window.innerHeight || 800;
              let best = null, bestBottom = 0;
              for (const el of document.querySelectorAll(
                "textarea, [contenteditable='true'], [role='textbox']"
              )) {
                const r = el.getBoundingClientRect();
                if (r.width < 80 || r.height < 20) continue;
                if (r.bottom < vh * 0.35) continue;
                if (r.bottom > bestBottom) {
                  bestBottom = r.bottom;
                  best = {x: r.x, y: r.y, w: r.width, h: r.height};
                }
              }
              return best;
            }"""
        )
    except Exception:
        box = None
    if not box:
        return None
    return {
        "x": float(box["x"]),
        "y": float(box["y"]),
        "w": float(box["w"]),
        "h": float(box["h"]),
    }


async def _focus_composer(page: Any) -> bool:
    """입력창 중앙을 눌러 포커스. 우하단(마이크·전송)은 누르지 않는다."""
    box = await _composer_box(page)
    if not box:
        return False
    x = box["x"] + min(box["w"] * 0.4, 120)
    y = box["y"] + box["h"] * 0.45
    try:
        await page.mouse.click(x, y)
        await page.wait_for_timeout(120)
        return True
    except Exception:
        return False


async def _click_composer_send_geo(page: Any) -> bool:
    """입력창 같은 줄의 전송 버튼을 눌러 보낸다.

    우하단 좌표 클릭은 마이크·스크롤 FAB 를 눌러 포커스만 빼므로 쓰지 않는다.
    """
    try:
        hit = await page.evaluate(
            """() => {
              const vh = window.innerHeight || 800;
              let editor = null, bestBottom = 0;
              for (const el of document.querySelectorAll(
                "textarea, [contenteditable='true'], [role='textbox']"
              )) {
                const r = el.getBoundingClientRect();
                if (r.width < 80 || r.height < 20) continue;
                if (r.bottom < vh * 0.35) continue;
                if (r.bottom > bestBottom) {
                  bestBottom = r.bottom;
                  editor = el;
                }
              }
              if (!editor) return 'no-editor';
              const er = editor.getBoundingClientRect();
              const lab = (el) => (
                (el.getAttribute('aria-label') || '') + ' '
                + (el.getAttribute('title') || '') + ' '
                + (el.innerText || '')
              ).toLowerCase();
              const skipLab = /generate|새\\s*이미지|new\\s*image|attach|upload|plus|\\+|mic|micro|voice|음성|녹음|file|clip|emoji|스크롤|scroll|add\\s*photo/;
              const isOverlayFab = (el, r) => {
                const st = getComputedStyle(el);
                if (st.position !== 'fixed' && st.position !== 'sticky')
                  return false;
                if (Math.abs(r.bottom - er.bottom) > 48) return true;
                if (r.left > er.left + 48 && r.right < er.right - 48)
                  return true;
                return false;
              };
              const enable = (el) => {
                try {
                  el.removeAttribute('disabled');
                  el.disabled = false;
                  el.setAttribute('aria-disabled', 'false');
                } catch (e) {}
              };
              const clickBtn = (el, why) => {
                enable(el);
                const r = el.getBoundingClientRect();
                return {
                  hit: why,
                  x: r.x + r.width / 2,
                  y: r.y + r.height / 2,
                };
              };
              const nodes = Array.from(document.querySelectorAll(
                'button, [role="button"]'
              ));
              for (const el of nodes) {
                const r = el.getBoundingClientRect();
                if (r.width < 16 || r.width > 96 || r.height < 16 || r.height > 96)
                  continue;
                if (isOverlayFab(el, r)) continue;
                if (Math.abs(r.bottom - er.bottom) > 72) continue;
                const t = lab(el);
                if (!/send|전송|submit|보내기|보내/.test(t)) continue;
                if (skipLab.test(t)) continue;
                return clickBtn(el, 'el-click-label');
              }
              let root = editor.closest(
                'form, [class*="composer" i], [class*="prompt" i], footer'
              ) || editor.parentElement;
              for (let i = 0; i < 6 && root && root.parentElement; i++) {
                const pr = root.getBoundingClientRect();
                if (pr.width > (window.innerWidth || 1200) * 0.96) break;
                if (pr.height > 320) break;
                const nxt = root.parentElement;
                const nr = nxt.getBoundingClientRect();
                if (nr.height > 360) break;
                root = nxt;
              }
              const hits = [];
              const scope = (root || document).querySelectorAll(
                'button, [role="button"]'
              );
              for (const el of scope) {
                const r = el.getBoundingClientRect();
                if (r.width < 16 || r.width > 96 || r.height < 16 || r.height > 96)
                  continue;
                if (Math.abs(r.bottom - er.bottom) > 72) continue;
                if (isOverlayFab(el, r)) continue;
                const t = lab(el);
                if (skipLab.test(t)) continue;
                if (r.left < er.right - 160) continue;
                if (r.left > er.right + 120) continue;
                hits.push({el, x: r.x + r.width / 2, y: r.y + r.height / 2});
              }
              hits.sort((a, b) => b.x - a.x);
              if (hits.length)
                return clickBtn(hits[0].el, 'el-click-right');
              // 아이콘 전용(→) 전송 — Genspark agents composer 우하단
              const iconHits = [];
              for (const el of nodes) {
                const r = el.getBoundingClientRect();
                if (r.width < 22 || r.width > 68 || r.height < 22 || r.height > 68)
                  continue;
                if (Math.abs(r.bottom - er.bottom) > 88) continue;
                if (r.left < er.left + er.width * 0.35) continue;
                if (isOverlayFab(el, r)) continue;
                const t = lab(el);
                if (skipLab.test(t)) continue;
                const hasSvg = !!(el.querySelector && el.querySelector('svg'))
                  || el.tagName === 'svg';
                const txt = (el.innerText || '').replace(/\\s+/g, '').trim();
                if (!hasSvg && txt.length > 6) continue;
                if (/generate|새\\s*이미지|new\\s*image|mic|voice|음성|녹음/.test(t))
                  continue;
                iconHits.push({el, x: r.x + r.width / 2, y: r.y + r.height / 2});
              }
              iconHits.sort((a, b) => b.x - a.x);
              if (iconHits.length)
                return clickBtn(iconHits[0].el, 'el-click-icon');
              const form = editor.closest('form');
              if (form && typeof form.requestSubmit === 'function') {
                form.requestSubmit();
                return {hit: 'form-submit', x: 0, y: 0};
              }
              return {hit: 'no-btn', x: 0, y: 0};
            }"""
        )
    except Exception:
        hit = ""
    why = ""
    x = y = 0.0
    if isinstance(hit, dict):
        why = str(hit.get("hit") or "")
        x = float(hit.get("x") or 0)
        y = float(hit.get("y") or 0)
    else:
        why = str(hit or "")
    if why in (
        "el-click-label",
        "el-click-right",
        "el-click-icon",
        "el-click",
    ) and x > 0 and y > 0:
        try:
            await page.mouse.click(x, y)
            await page.wait_for_timeout(280)
            _tab_log(f"submit geo={why} x={int(x)} y={int(y)}")
            return True
        except Exception:
            pass
    if why == "form-submit":
        await page.wait_for_timeout(280)
        _tab_log("submit geo=form-submit")
        return True
    _tab_log(f"submit geo 실패 hit={why or '-'}")
    return False


async def _click_followup_send(page: Any) -> bool:
    """이어쓰기 칸 옆 Send만 클릭 (Generate·새 이미지 제외)."""
    editors = await _prompt_editor_candidates(page, prefer_followup=True)
    if not editors:
        editors = await _prompt_editor_candidates(page, prefer_followup=False)
    box = None
    if editors:
        try:
            await editors[0].click(timeout=2000)
        except Exception:
            pass
        try:
            box = await editors[0].bounding_box()
        except Exception:
            box = None
    y0 = float(box.get("y", 0)) if box else None
    for sel in (
        "button:has-text('Send')",
        "button:has-text('전송')",
        "button:has-text('Submit')",
        "button:has-text('실행')",
        "[aria-label*='Send' i]",
        "[aria-label*='전송' i]",
        "[aria-label*='Send message' i]",
        "button[type='submit']:visible",
        "button[class*='send' i]",
    ):
        loc = page.locator(sel)
        try:
            n = await loc.count()
        except Exception:
            continue
        for i in range(min(n, 8)):
            btn = loc.nth(i)
            try:
                if not await btn.is_visible(timeout=600):
                    continue
                label = (
                    (await btn.inner_text(timeout=400) or "")
                    + " "
                    + (await btn.get_attribute("aria-label") or "")
                ).lower()
                if re.search(r"generate|새\s*이미지|new\s*image|create", label):
                    continue
                bb = await btn.bounding_box()
                if not bb:
                    continue
                if y0 is not None and abs(float(bb.get("y", 0)) - y0) > 140:
                    continue
                try:
                    await btn.evaluate(
                        """el => {
                          el.removeAttribute('disabled');
                          el.disabled = false;
                          el.setAttribute('aria-disabled', 'false');
                        }"""
                    )
                except Exception:
                    pass
                await btn.click(timeout=4000, force=True)
                return True
            except Exception:
                continue
    return await _click_composer_send_geo(page)


async def _click_composer_send_for_editor(page: Any, editor: Any) -> bool:
    """editor 컨테이너 안 전송(→) 버튼 — ai_image 랜딩용."""
    try:
        hit = await editor.evaluate(
            """(ed) => {
              if (!ed) return {hit: 'no-editor', x: 0, y: 0};
              const er = ed.getBoundingClientRect();
              let container = ed.closest(
                '[class*="composer" i],[class*="prompt" i],'
                + '[class*="input-area" i],[class*="chat-input" i],form,footer'
              ) || ed.parentElement;
              for (let i = 0; i < 8 && container; i++) {
                const r = container.getBoundingClientRect();
                if (r.width > 220 && r.height > 72 && r.height < 720) break;
                container = container.parentElement;
              }
              if (!container) container = ed.parentElement;
              const cr = container.getBoundingClientRect();
              const lab = (el) => (
                (el.getAttribute('aria-label') || '') + ' '
                + (el.getAttribute('title') || '') + ' '
                + (el.innerText || '')
              ).toLowerCase();
              const skipLab = /mic|micro|voice|음성|attach|upload|plus|\\+|file|clip|emoji|scroll|generate|새\\s*이미지|new\\s*image|add\\s*photo/i;
              const enable = (el) => {
                try {
                  el.removeAttribute('disabled');
                  el.disabled = false;
                  el.setAttribute('aria-disabled', 'false');
                } catch (e) {}
              };
              const pick = (el, why, bonus) => {
                enable(el);
                const r = el.getBoundingClientRect();
                return {
                  hit: why,
                  x: r.x + r.width / 2,
                  y: r.y + r.height / 2,
                  score: r.left * 2 + bonus,
                };
              };
              const cands = [];
              const nodes = container.querySelectorAll(
                'button, [role="button"], div, span, a'
              );
              for (const el of nodes) {
                if (el === ed || ed.contains(el)) continue;
                const r = el.getBoundingClientRect();
                if (r.width < 18 || r.width > 88 || r.height < 18 || r.height > 88)
                  continue;
                if (r.bottom < er.bottom - 56 || r.bottom > cr.bottom + 24)
                  continue;
                if (r.left < er.left + er.width * 0.42) continue;
                const t = lab(el);
                if (skipLab.test(t)) continue;
                const st = getComputedStyle(el);
                if (st.pointerEvents === 'none' || st.visibility === 'hidden')
                  continue;
                const hasSvg = !!(el.querySelector && el.querySelector('svg'));
                const clickable = el.tagName === 'BUTTON'
                  || el.getAttribute('role') === 'button'
                  || st.cursor === 'pointer'
                  || hasSvg;
                if (!clickable) continue;
                let bonus = hasSvg ? 40 : 0;
                if (/send|submit|전송|보내/.test(t)) bonus += 120;
                cands.push(pick(el, 'editor-send', bonus));
              }
              cands.sort((a, b) => b.score - a.score);
              if (cands.length) return cands[0];
              const fx = cr.right - 34;
              const fy = Math.min(cr.bottom - 30, er.bottom + 36);
              return {hit: 'editor-coord', x: fx, y: fy, score: 0};
            }"""
        )
    except Exception:
        hit = None
    why = ""
    x = y = 0.0
    if isinstance(hit, dict):
        why = str(hit.get("hit") or "")
        x = float(hit.get("x") or 0)
        y = float(hit.get("y") or 0)
    if why in ("editor-send", "editor-coord") and x > 0 and y > 0:
        try:
            await page.mouse.click(x, y)
            await page.wait_for_timeout(280)
            _tab_log(f"첨부: send {why} x={int(x)} y={int(y)}")
            return True
        except Exception:
            pass
    _tab_log(f"첨부: send 실패 hit={why or '-'}")
    return False


async def _editor_submit_enter(page: Any, editor: Any) -> None:
    """agents·랜딩 composer — Enter 전송."""
    await _focus_composer_editor(page, editor)
    await page.wait_for_timeout(80)
    try:
        await editor.press("Enter", timeout=4000)
        _tab_log("첨부: editor.press Enter")
    except Exception as ex:
        _tab_log(f"첨부: Enter 실패 → keyboard · {ex}")
        await page.keyboard.press("Enter")


async def _ctrl_enter_submit_editor(page: Any, editor: Any) -> None:
    """대화(agents) composer — Control+Enter 전송."""
    await _focus_composer_editor(page, editor)
    await page.wait_for_timeout(80)
    try:
        await editor.press("Control+Enter", timeout=4000)
        _tab_log("첨부: editor.press Control+Enter")
        return
    except Exception as ex:
        _tab_log(f"첨부: editor.press 실패 → keyboard · {ex}")
    await _ctrl_enter_submit(page)


async def _raise_if_page_limited(page: Any, *, label: str = "") -> None:
    """한도 배너·토스트가 보이면 ``AiImageLimitError`` (``reset_at`` 포함)."""
    hit = await detect_limit_on_page(page)
    if hit is not None:
        if label:
            _tab_log(
                f"한도감지 {label} · {hit.message} · {hit.snippet[:120]}"
            )
        raise_limit_error(hit)


async def _submit_after_attach(page: Any, editor: Any) -> None:
    """첨부 후 1초 대기 → Enter·전송버튼·Ctrl+Enter 순 전송."""
    await _raise_if_page_limited(page, label="첨부후전송")
    before = await _composer_text_len(page)
    before_thumbs = await _composer_pending_thumb_count(page, editor)
    page_sc = _score_ai_image_url(page.url or "")
    on_agents = page_sc >= 40
    if on_agents:
        methods: tuple[str, ...] = ("enter", "geo", "ctrl", "send")
    else:
        methods = ("geo", "enter", "send", "ctrl")
    _tab_log(
        f"첨부 후 {ATTACH_SUBMIT_DELAY_MS}ms 대기 → 전송 "
        f"composer={before} thumbs={before_thumbs} "
        f"methods={','.join(methods)} url={(page.url or '')[:100]}"
    )
    await page.wait_for_timeout(ATTACH_SUBMIT_DELAY_MS)
    thumbs_now = await _composer_pending_thumb_count(page, editor)
    attach_score = await _composer_attachment_score(page, editor)
    chip_visible = False
    if thumbs_now == 0 and attach_score == 0:
        for _ in range(8):
            await page.wait_for_timeout(350)
            thumbs_now = await _composer_pending_thumb_count(page, editor)
            attach_score = await _composer_attachment_score(page, editor)
            chip_visible = await _composer_paste_chip_visible(page, editor)
            if thumbs_now > 0 or attach_score > 0 or chip_visible:
                break
    else:
        chip_visible = await _composer_paste_chip_visible(page, editor)
    if thumbs_now == 0 and attach_score == 0 and not chip_visible:
        _tab_log(
            "첨부: 전송 전 미탐지 — Ctrl+V·클립보드 OK 기준 전송 계속"
        )
    before_thumbs = max(before_thumbs, thumbs_now)
    _tab_log(
        f"첨부: 전송 전 확인 thumbs={thumbs_now} score={attach_score} "
        f"chip={chip_visible}"
    )
    skip_keys: set[str] = set()
    tries = 5
    for i in range(tries):
        if i > 0:
            _tab_log(f"첨부: 전송 재시도 {i + 1}/{tries}")
            await _focus_composer_editor(page, editor)
        for name in methods:
            if name in skip_keys:
                continue
            len_before = await _composer_text_len(page)
            if name == "enter":
                await _editor_submit_enter(page, editor)
            elif name == "ctrl":
                await _ctrl_enter_submit_editor(page, editor)
            elif name == "geo":
                await _click_composer_send_for_editor(page, editor)
            elif name == "send":
                await _click_followup_send(page)
            await page.wait_for_timeout(600)
            ok, reason = await _submit_sent_ok(
                page,
                before_len=before,
                after_attach=True,
                editor=editor,
                before_thumbs=before_thumbs,
            )
            if ok:
                after = await _composer_text_len(page)
                _tab_log(
                    f"첨부: 전송 확인 method={name} reason={reason} "
                    f"composer={after}"
                )
                return
            len_after = await _composer_text_len(page)
            if name in ("enter", "ctrl") and len_after > len_before + 1:
                skip_keys.add(name)
                _tab_log(
                    f"첨부: {name} → composer +{len_after - len_before} "
                    "(줄바꿈) — 해당 키 생략"
                )
            _tab_log(
                f"첨부: {name} 미확인 generating="
                f"{await _is_generating(page)} composer={len_after}"
            )
    n = await _composer_text_len(page)
    raise RuntimeError(
        f"첨부 후 전송 실패 — 입력창에 명령이 {n}자 남아 있습니다."
    )


async def _ctrl_enter_submit(page: Any) -> None:
    """Genspark 채팅은 Enter=줄바꿈, Ctrl+Enter=전송인 경우가 많음."""
    try:
        await page.keyboard.press("Control+Enter")
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
    except Exception:
        pass


async def _submit(page: Any, *, after_attach: bool = False) -> bool:
    """이어쓰기 전송 — Enter / Ctrl+Enter (agents).

    ``after_attach`` — 전송 버튼 탐색 없이 **Ctrl+Enter 만** 사용.
    """
    before = await _composer_text_len(page)
    _tab_log(
        f"submit 시작 composer={before} after_attach={after_attach} "
        f"url={(page.url or '')[:120]}"
    )
    if before < 8:
        _tab_log("submit 건너뜀: 입력창 비어 있음")
        return False
    on_agents = _score_ai_image_url(page.url or "") >= 40
    if after_attach:
        cands = await _prompt_editor_candidates(
            page, prefer_followup=on_agents
        )
        if not cands:
            cands = await _prompt_editor_candidates(
                page, prefer_followup=False
            )
        if cands:
            await _focus_composer_editor(page, cands[0])
        else:
            await _focus_composer(page)
        methods: tuple[str, ...] = ("ctrl",)
        settle_ms = 500
    elif on_agents:
        await _focus_composer(page)
        methods = ("enter", "ctrl", "send", "geo")
        settle_ms = 280
    else:
        await _focus_composer(page)
        methods = ("geo", "send", "ctrl", "enter")
        settle_ms = 280
    for name in methods:
        if name in ("ctrl", "enter") and not after_attach:
            await _focus_composer(page)
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass
            await page.wait_for_timeout(60)
        if name == "geo":
            hit = await _click_composer_send_geo(page)
            _tab_log(f"submit 시도 method={name} geo={hit}")
        elif name == "send":
            hit = await _click_followup_send(page)
            _tab_log(f"submit 시도 method={name} send={hit}")
        elif name == "ctrl":
            await _ctrl_enter_submit(page)
            _tab_log(
                f"submit 시도 method={name}"
                + (" (첨부직후 Ctrl+Enter)" if after_attach else "")
            )
        else:
            await page.keyboard.press("Enter")
            _tab_log(f"submit 시도 method={name}")
        await page.wait_for_timeout(settle_ms)
        after = await _composer_text_len(page)
        ok, reason = await _submit_sent_ok(
            page, before_len=before, after_attach=after_attach
        )
        if ok:
            _tab_log(
                f"submit 확인 method={name} reason={reason} "
                f"composer={after}"
            )
            return True
        _tab_log(
            f"submit 미확인 method={name} generating={await _is_generating(page)} "
            f"composer={after}"
        )
    _tab_log(f"submit 실패 composer잔존={await _composer_text_len(page)}")
    return False


async def _ensure_submitted(page: Any, *, after_attach: bool = False) -> None:
    """전송될 때까지 재시도. ``after_attach`` — Ctrl+Enter 만·판정 완화."""
    await _raise_if_page_limited(page, label="전송전")
    tries = 5 if after_attach else 2
    if after_attach:
        await page.wait_for_timeout(200)
        await _scroll_to_composer(page)
        await _focus_composer(page)
    for i in range(tries):
        if await _submit(page, after_attach=after_attach):
            return
        if i < tries - 1:
            _tab_log(f"submit 재시도 {i + 2}/{tries}")
        await _focus_composer(page)
        await page.wait_for_timeout(450 if after_attach else 300)
    n = await _composer_text_len(page)
    raise RuntimeError(
        f"이어쓰기 전송 실패 — 입력창에 명령이 {n}자 남아 있습니다."
    )


async def _click_by_text(page: Any, texts: tuple[str, ...]) -> bool:
    for text in texts:
        # 넓은 div/span 보다 버튼·옵션을 우선 — 파일 input 오클릭 방지
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
                if await _is_file_upload_target(loc):
                    continue
                await loc.click(timeout=4000, force=False)
                await page.wait_for_timeout(600)
                return True
            except Exception:
                continue
    return False


def _attach_filechooser_guard(page: Any) -> None:
    """실수로 뜬 파일 선택 창은 즉시 빈 선택으로 닫는다.

    의도적 첨부(``_FC_ALLOW``) 중에는 가로채지 않는다.
    """

    async def _on_chooser(chooser: Any) -> None:
        if _FC_ALLOW["v"]:
            return
        try:
            await chooser.set_files([])
        except Exception:
            pass

    try:
        page.on("filechooser", lambda c: asyncio.create_task(_on_chooser(c)))
    except Exception:
        pass


def _iter_page_targets(page: Any) -> list[Any]:
    out: list[Any] = [page]
    try:
        for fr in page.frames:
            if fr is not page.main_frame:
                out.append(fr)
    except Exception:
        pass
    return out


async def _try_set_files_on_target(target: Any, paths: list[str]) -> bool:
    for sel in ("input[type='file']", "input[type='file'][multiple]"):
        loc = target.locator(sel)
        try:
            n = await loc.count()
        except Exception:
            n = 0
        for i in range(min(n, 12)):
            item = loc.nth(i)
            try:
                await item.wait_for(state="attached", timeout=3000)
                await item.set_input_files(paths, timeout=12_000)
                await asyncio.sleep(0.8)
                return True
            except Exception:
                continue
    return False


async def _add_entry_near_editor(
    btn: Any, editor: Any | None, *, relaxed: bool = False
) -> bool:
    """``+`` 버튼이 지정 입력창(composer) 옆인지."""
    if editor is None:
        return True
    max_dy = 96 if relaxed else 72
    max_left = 220 if relaxed else 120
    try:
        return bool(
            await btn.evaluate(
                """(btn, ed, maxDy, maxLeft) => {
                  if (!btn || !ed) return false;
                  const br = btn.getBoundingClientRect();
                  const er = ed.getBoundingClientRect();
                  if (Math.abs(br.bottom - er.bottom) > maxDy) return false;
                  if (br.right > er.left + 88) return false;
                  if (br.left < er.left - maxLeft) return false;
                  return true;
                }""",
                editor,
                max_dy,
                max_left,
            )
        )
    except Exception:
        return False


async def _page_background_processing(page: Any) -> bool:
    """Genspark 「백그라운드 처리 중」 — 첨부·입력 전 대기."""
    try:
        return bool(
            await page.evaluate(
                """() => {
                  const t = ((document.body && document.body.innerText) || '')
                    .slice(-4500);
                  return /백그라운드[^\\n]{0,24}처리\\s*중|아직\\s*처리\\s*중|
                    still\\s*being\\s*processed|processing\\s*in\\s*the\\s*background/i
                    .test(t);
                }"""
            )
        )
    except Exception:
        return False


async def _page_request_interrupted(page: Any) -> bool:
    """「요청이 중단되었습니다」 — 직전 생성이 아직 정리 중."""
    try:
        return bool(
            await page.evaluate(
                """() => {
                  const t = ((document.body && document.body.innerText) || '')
                    .slice(-3500);
                  return /요청이\\s*중단|request\\s*(?:was\\s*)?interrupt|stopped\\s*generating/i
                    .test(t);
                }"""
            )
        )
    except Exception:
        return False


async def _scroll_to_composer(page: Any) -> None:
    """채팅 하단·composer 로 스크롤."""
    try:
        await page.evaluate(
            """() => {
              const h = document.body && document.body.scrollHeight || 0;
              window.scrollTo(0, Math.max(0, h));
              for (const el of document.querySelectorAll(
                '[class*="chat" i],[class*="message" i],[class*="conversation" i],main'
              )) {
                try {
                  if (el.scrollHeight > el.clientHeight + 40)
                    el.scrollTop = el.scrollHeight;
                } catch (e) {}
              }
            }"""
        )
        await page.wait_for_timeout(280)
    except Exception:
        pass


async def _wait_until_generation_idle(
    page: Any,
    *,
    timeout_sec: float = 120.0,
    stable_hits: int = 2,
    label: str = "",
    watch_srt_sec: int | None = None,
) -> bool:
    """이미지 생성·백그라운드 처리 종료까지 (첨부·명령 입력 **전** 필수)."""
    deadline = time.time() + max(5.0, float(timeout_sec))
    hits = 0
    tag = label or "idle"
    last_busy_log = 0.0
    while time.time() < deadline:
        try:
            if watch_srt_sec is not None:
                ws = int(watch_srt_sec)
                if await _srt_success_message_seen(page, ws):
                    near = (
                        await _file_src_near_srt_label(page, ws) or ""
                    ).strip()
                    if near and "api/files" in near.lower():
                        if not await _page_background_processing(page):
                            cands = await _prompt_editor_candidates(
                                page, prefer_followup=True
                            )
                            if not cands:
                                cands = await _prompt_editor_candidates(
                                    page, prefer_followup=False
                                )
                            if cands:
                                hits += 1
                                if hits >= max(1, int(stable_hits)):
                                    _tab_log(f"생성완료대기 OK · {tag}")
                                    return True
                                await page.wait_for_timeout(400)
                                continue
            pending = await _chat_has_pending_image_generation(page)
            if watch_srt_sec is not None and pending:
                ws = int(watch_srt_sec)
                if await _srt_success_message_seen(page, ws):
                    near = (
                        await _file_src_near_srt_label(page, ws) or ""
                    ).strip()
                    if near and "api/files" in near.lower():
                        pending = False
            phantom = False
            if watch_srt_sec is not None:
                phantom = await _srt_success_without_image(
                    page, int(watch_srt_sec)
                )
            interrupted = await _page_request_interrupted(page)
            busy = (
                pending
                or phantom
                or interrupted
                or await _is_generating(page)
                or await _page_background_processing(page)
            )
            if busy:
                hits = 0
                now = time.time()
                if now - last_busy_log >= 8.0:
                    last_busy_log = now
                    if phantom:
                        reason = f"성공문구만 SRT_{int(watch_srt_sec):03d}"
                    elif interrupted:
                        reason = "요청중단"
                    elif pending:
                        reason = "이미지생성카드"
                    else:
                        reason = "생성중"
                    _tab_log(f"생성완료대기 · {tag} — {reason}")
                await page.wait_for_timeout(500)
                continue
            cands = await _prompt_editor_candidates(
                page, prefer_followup=True
            )
            if not cands:
                cands = await _prompt_editor_candidates(
                    page, prefer_followup=False
                )
            if not cands:
                hits = 0
                await page.wait_for_timeout(400)
                continue
            hits += 1
            if hits >= max(1, int(stable_hits)):
                _tab_log(f"생성완료대기 OK · {tag}")
                return True
        except Exception:
            hits = 0
        await page.wait_for_timeout(400)
    _tab_log(f"생성완료대기 시간초과 · {tag}")
    return False


async def _wait_composer_ready_for_attach(
    page: Any, *, timeout_sec: float = 120.0
) -> bool:
    """생성 종료 + composer (Ctrl+V 첨부 전)."""
    return await _wait_until_generation_idle(
        page, timeout_sec=timeout_sec, stable_hits=2, label="첨부composer"
    )


async def _wait_attach_ready(
    page: Any, *, timeout_sec: float = 90.0
) -> bool:
    """레거시 — filechooser 폴백용."""
    return await _wait_until_generation_idle(
        page, timeout_sec=timeout_sec, stable_hits=2, label="첨부"
    )


def _agent_id_from_url(url: str) -> str:
    m = re.search(r"[?&]id=([^&#]+)", (url or "").strip())
    return (m.group(1) if m else "").strip()


async def _ensure_attach_page(
    context: Any | None, page: Any, work_url: str = ""
) -> Any:
    """참조 첨부는 ``agents?id=`` 대화 탭에서만."""
    wu = (work_url or _ATTACH_WORK_URL.get("v") or "").strip()
    aid = _agent_id_from_url(wu)
    if context is not None and aid:
        for p in list(context.pages or []):
            try:
                if p.is_closed():
                    continue
                if aid in (p.url or ""):
                    await p.bring_to_front()
                    await _install_page_guards(p)
                    return p
            except Exception:
                continue
    try:
        if page is not None and not page.is_closed():
            if not aid or aid in (page.url or ""):
                await page.bring_to_front()
                return page
    except Exception:
        pass
    if context is not None:
        return await _alive_page(context, page)
    return page


async def _set_files_on_any_page_input(page: Any, paths: list[str]) -> bool:
    """페이지 ``input[type=file]`` 전체에 주입 (``+`` 메뉴 연 뒤)."""
    fname = Path(paths[0]).name if paths else "?"
    try:
        loc = page.locator("input[type=file]")
        n = await loc.count()
        for i in range(min(n, 16)):
            item = loc.nth(i)
            try:
                await item.set_input_files(paths, timeout=8000)
                await page.wait_for_timeout(500)
                _tab_log(f"첨부: page input[file] OK · {fname} n={n}")
                return True
            except Exception:
                continue
    except Exception as ex:
        _tab_log(f"첨부: page input[file] 실패 {ex}")
    return False


async def _click_local_file_menu_item(page: Any) -> bool:
    """업로드 메뉴 항목 클릭 (새 채팅·드라이브 제외). filechooser 는 호출측에서."""
    for text in (
        "로컬 파일 찾기",
        "로컬 파일",
        "Find local file",
        "Upload from computer",
        "Upload file",
        "Upload image",
        "컴퓨터에서",
        "내 기기",
        "Browse",
    ):
        for sel in (
            f"[role='menuitem']:has-text('{text}')",
            f"[role='option']:has-text('{text}')",
            f"button:has-text('{text}')",
            f"li:has-text('{text}')",
        ):
            item = page.locator(sel).first
            try:
                if not await item.is_visible(timeout=800):
                    continue
                txt = (await item.inner_text() or "").strip()
                if re.search(r"새\s*채팅|new\s*chat|드라이브", txt, re.I):
                    continue
                await item.click(timeout=3000)
                _tab_log(f"첨부: 메뉴 '{text}' 클릭")
                return True
            except Exception:
                continue
    try:
        ok = bool(
            await page.evaluate(
                """() => {
                  const skip = /새\\s*채팅|new\\s*chat|드라이브|drive|google/i;
                  const hit = /로컬|local|upload|browse|컴퓨터|기기|computer|device|file|이미지|image|photo|사진/i;
                  for (const el of document.querySelectorAll(
                    '[role="menuitem"],[role="option"],button,li,div,span'
                  )) {
                    const t = (el.innerText || el.textContent || '').trim();
                    if (!t || t.length > 80 || skip.test(t)) continue;
                    if (!hit.test(t)) continue;
                    if (/video|동영상|영상/.test(t)) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width < 20 || r.height < 12) continue;
                    if (r.top < 40 || r.bottom > (window.innerHeight || 800) - 20)
                      continue;
                    el.click();
                    return true;
                  }
                  return false;
                }"""
            )
        )
        if ok:
            _tab_log("첨부: 업로드 메뉴 (evaluate) 클릭")
        return ok
    except Exception:
        return False


def _copy_image_to_clipboard_win(image_path: str) -> bool:
    """Windows 클립보드에 PNG/JPG 이미지 복사 (Ctrl+V 첨부용)."""
    if sys.platform != "win32":
        return False
    p = Path(image_path).resolve()
    if not p.is_file():
        return False
    path_esc = str(p).replace("'", "''")
    for ps in (
        (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "Add-Type -AssemblyName System.Drawing; "
            f"$img=[System.Drawing.Image]::FromFile('{path_esc}'); "
            "[System.Windows.Forms.Clipboard]::SetImage($img); "
            "$img.Dispose()"
        ),
        f"Set-Clipboard -Path '{path_esc}'",
    ):
        try:
            r = subprocess.run(
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
                timeout=15,
                check=False,
                creationflags=_subprocess_no_window_flags(),
            )
            if r.returncode == 0:
                return True
        except (OSError, subprocess.TimeoutExpired):
            continue
    return False


async def _composer_attachment_score(
    page: Any, editor: Any, *, fname_hint: str = ""
) -> int:
    """composer 입력창 **대기 중** 첨부 미리보기 점수 (채팅 히스토리 제외)."""
    hint = (fname_hint or "").strip()
    try:
        return int(
            await page.evaluate(
                """(args) => {
                  const [ed, hint] = args;
                  if (!ed) return 0;
                  const er = ed.getBoundingClientRect();
                  const root = ed.closest(
                    '[class*="composer" i],[class*="chat-input" i],'
                    + 'footer,form,aside'
                  ) || ed.parentElement || document.body;
                  let score = 0;
                  const near = (r) => (
                    r.width >= 12 && r.height >= 12
                    && r.bottom >= er.top - 180 && r.top <= er.top + 48
                    && Math.abs(r.left - er.left) < 640
                  );
                  for (const el of root.querySelectorAll(
                    'img,[class*="attachment" i],[class*="preview" i],'
                    + '[class*="thumbnail" i],[class*="uploaded" i],'
                    + '[class*="file-chip" i],[class*="image-preview" i],'
                    + '[class*="upload" i],[class*="pending" i],'
                    + '[class*="chip" i],[class*="file" i]'
                  )) {
                    const r = el.getBoundingClientRect();
                    if (!near(r)) continue;
                    score++;
                  }
                  const rootText = (root.innerText || root.textContent || '');
                  if (hint && rootText.includes(hint)) score += 4;
                  if (/\\.(png|jpg|jpeg|webp)\\b/i.test(rootText.slice(-800)))
                    score += 2;
                  const html = ed.innerHTML || ed.value || '';
                  if (/<img[\\s>]/i.test(html)) score += 2;
                  for (const inp of root.querySelectorAll('input[type=file]')) {
                    if (inp.files && inp.files.length) score += 3;
                  }
                  return score;
                }""",
                [editor, hint],
            )
        )
    except Exception:
        return 0


async def _composer_has_pending_attachment(
    page: Any, editor: Any, *, fname_hint: str = ""
) -> bool:
    """composer에 아직 전송 전 첨부가 있는지."""
    if await _composer_pending_thumb_count(page, editor) > 0:
        return True
    return (
        await _composer_attachment_score(page, editor, fname_hint=fname_hint)
    ) > 0


async def _composer_pending_thumb_count(page: Any, editor: Any) -> int:
    """composer 입력란 **대기 중** 썸네일 개수 (채팅 히스토리 제외)."""
    try:
        return int(
            await page.evaluate(
                """(ed) => {
                  if (!ed) return 0;
                  const er = ed.getBoundingClientRect();
                  const root = ed.closest(
                    '[class*="composer" i],[class*="chat-input" i],'
                    + '[class*="input-area" i],footer,form'
                  ) || ed.parentElement?.parentElement || ed.parentElement;
                  if (!root) return 0;
                  const rr = root.getBoundingClientRect();
                  const inStrip = (r) => (
                    r.width >= 36 && r.height >= 36
                    && r.width <= 240 && r.height <= 240
                    && r.top >= rr.top - 12 && r.bottom <= er.bottom + 16
                    && r.left >= rr.left - 12 && r.right <= rr.right + 12
                  );
                  const seen = new Set();
                  let n = 0;
                  const bump = (r) => {
                    const key = Math.round(r.left) + ':'
                      + Math.round(r.top) + ':' + Math.round(r.width);
                    if (seen.has(key)) return;
                    seen.add(key);
                    n++;
                  };
                  for (const img of root.querySelectorAll('img')) {
                    const r = img.getBoundingClientRect();
                    if (!inStrip(r)) continue;
                    const src = (img.currentSrc || img.src || '').toLowerCase();
                    if (/avatar|icon|logo|emoji|favicon|add-entry|svg\\+xml/.test(src))
                      continue;
                    bump(r);
                  }
                  for (const el of root.querySelectorAll(
                    '[class*="attachment" i],[class*="preview" i],'
                    + '[class*="thumbnail" i],[class*="upload" i],'
                    + '[class*="file" i],[class*="chip" i],canvas'
                  )) {
                    const r = el.getBoundingClientRect();
                    if (!inStrip(r)) continue;
                    bump(r);
                  }
                  for (const el of root.querySelectorAll('div,span')) {
                    const st = getComputedStyle(el);
                    const bg = st.backgroundImage || '';
                    if (!bg || bg === 'none') continue;
                    const r = el.getBoundingClientRect();
                    if (!inStrip(r)) continue;
                    bump(r);
                  }
                  return n;
                }""",
                editor,
            )
        )
    except Exception:
        return 0


async def _clear_composer_attachments(
    page: Any, editor: Any, *, clear_clipboard: bool = True
) -> None:
    """composer 대기 중 첨부·메뉴·(선택) 클립보드 초기화."""
    if clear_clipboard:
        _clear_clipboard_win()
    for _ in range(2):
        try:
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(120)
        except Exception:
            break
    for _pass in range(6):
        try:
            await page.evaluate(
                """(ed) => {
                  if (!ed) return;
                  const er = ed.getBoundingClientRect();
                  const root = ed.closest(
                    '[class*="composer" i],[class*="chat-input" i],footer,form'
                  ) || ed.parentElement || document.body;
                  for (const btn of root.querySelectorAll(
                    'button,[role="button"],[aria-label],[title],svg'
                  )) {
                    const node = btn.closest(
                      'button,[role=button]'
                    ) || btn;
                    const al = (
                      (node.getAttribute('aria-label') || '') + ' '
                      + (node.getAttribute('title') || '') + ' '
                      + (node.innerText || node.textContent || '')
                    );
                    const r = node.getBoundingClientRect();
                    if (r.width < 6 || r.height < 6) continue;
                    if (r.bottom < er.top - 320 || r.top > er.top + 12) continue;
                    const isClose = /remove|delete|close|clear|삭제|제거|닫|취소|×|✕/i.test(al)
                      || (r.width <= 30 && r.height <= 30
                        && r.bottom <= er.top + 8);
                    if (!isClose) continue;
                    try { node.click(); } catch (e) {}
                  }
                }""",
                editor,
            )
            await page.wait_for_timeout(180)
        except Exception:
            break
        n = await _composer_pending_thumb_count(page, editor)
        if n == 0 and not await _composer_has_pending_attachment(
            page, editor
        ):
            break
    if clear_clipboard:
        _clear_clipboard_win()


async def _ensure_composer_attachments_clear(
    page: Any, editor: Any, *, label: str = ""
) -> int:
    """첨부 썸네일 0장까지 제거. 남은 개수 반환."""
    for _ in range(5):
        n = await _composer_pending_thumb_count(page, editor)
        pending = await _composer_has_pending_attachment(page, editor)
        if n == 0 and not pending:
            return 0
        if label:
            _tab_log(
                f"첨부: 기존 {max(n, 1 if pending else 0)}장 제거 · {label}"
            )
        await _clear_composer_attachments(
            page, editor, clear_clipboard=False
        )
        await page.wait_for_timeout(250)
    return await _composer_pending_thumb_count(page, editor)


async def _composer_paste_chip_visible(
    page: Any, editor: Any, *, fname_hint: str = ""
) -> bool:
    """Genspark 랜딩 — pasted-text 칩·썸네일·파일명 (DOM 탐지 보완)."""
    hint = (fname_hint or "").strip()
    try:
        return bool(
            await page.evaluate(
                """(args) => {
                  const [ed, hint] = args;
                  if (!ed) return false;
                  const er = ed.getBoundingClientRect();
                  const root = ed.closest(
                    '[class*="composer" i],[class*="chat-input" i],'
                    + '[class*="input-area" i],[class*="prompt" i],form,footer'
                  ) || ed.parentElement?.parentElement || ed.parentElement;
                  if (!root) return false;
                  const rr = root.getBoundingClientRect();
                  const inComposer = (r) => (
                    r.width >= 20 && r.height >= 20
                    && r.bottom >= rr.top - 8 && r.top <= er.bottom + 48
                    && r.left >= rr.left - 16 && r.right <= rr.right + 16
                  );
                  for (const img of root.querySelectorAll('img')) {
                    const r = img.getBoundingClientRect();
                    if (!inComposer(r)) continue;
                    const src = (img.currentSrc || img.src || '').toLowerCase();
                    if (/avatar|icon|logo|emoji|favicon|add-entry|svg\\+xml/.test(src))
                      continue;
                    return true;
                  }
                  const txt = (root.innerText || root.textContent || '');
                  if (/pasted-text|\\.png|\\.jpg|jpeg|webp|\\d+\\.\\d+\\s*KB/i.test(txt))
                    return true;
                  if (hint) {
                    const stem = hint.replace(/\\.png$/i, '');
                    if (txt.includes(hint) || txt.includes(stem)) return true;
                  }
                  for (const el of root.querySelectorAll(
                    '[class*="attachment" i],[class*="preview" i],'
                    + '[class*="thumbnail" i],[class*="chip" i],[class*="file" i]'
                  )) {
                    const r = el.getBoundingClientRect();
                    if (inComposer(r)) return true;
                  }
                  return false;
                }""",
                [editor, hint],
            )
        )
    except Exception:
        return False


async def _paste_attach_verified(
    page: Any,
    editor: Any,
    *,
    before_count: int,
    before_score: int,
    fname_hint: str = "",
) -> tuple[int, bool]:
    """Ctrl+V 직후 첨부 확인 — 썸네일·점수·pending·pasted-text 칩."""
    after = await _composer_pending_thumb_count(page, editor)
    if after >= 1:
        return after, True
    score = await _composer_attachment_score(page, editor)
    if score > before_score + 1:
        return max(1, after), True
    if await _composer_has_pending_attachment(page, editor):
        return max(1, after), True
    if await _composer_paste_chip_visible(page, editor, fname_hint=fname_hint):
        return max(1, after), True
    return after, False


async def _wait_attach_verified(
    page: Any,
    editor: Any,
    *,
    before_count: int,
    before_score: int,
    fname: str = "",
    max_wait_sec: float = 3.0,
    poll_ms: int = 500,
) -> tuple[int, bool]:
    """첨부 후 썸네일·점수 확인 — ``poll_ms`` 간격 재시도."""
    deadline = time.time() + max(0.5, float(max_wait_sec))
    after, ok = 0, False
    while time.time() < deadline:
        after, ok = await _paste_attach_verified(
            page,
            editor,
            before_count=before_count,
            before_score=before_score,
            fname_hint=fname,
        )
        if ok:
            return after, True
        await page.wait_for_timeout(max(200, int(poll_ms)))
    after, ok = await _paste_attach_verified(
        page,
        editor,
        before_count=before_count,
        before_score=before_score,
        fname_hint=fname,
    )
    return after, ok


def _same_location_ref_skip(
    srt_path: str | Path | None,
    srt_sec: int,
    *,
    scene_prompt: str | None,
    interval_sec: int,
) -> bool:
    """같은 장소가 ``REF_SAME_LOCATION_MAX`` 회 연속이면 참조 첨부 생략."""
    loc_fp = scene_location_fingerprint(
        srt_path,
        srt_sec,
        scene_prompt=scene_prompt,
        interval_sec=interval_sec,
    )
    prev_fp = str(_REF_LOCATION_STREAK.get("fp") or "")
    prev_cnt = int(_REF_LOCATION_STREAK.get("count") or 0)
    if loc_fp and loc_fp == prev_fp:
        loc_cnt = prev_cnt + 1
    else:
        loc_cnt = 1
    _REF_LOCATION_STREAK["fp"] = loc_fp
    _REF_LOCATION_STREAK["count"] = loc_cnt
    if loc_fp and loc_cnt >= REF_SAME_LOCATION_MAX:
        _tab_log(
            f"참조첨부 생략 SRT_{srt_sec:03d} — "
            f"같은 장소 연속 {loc_cnt}회 · fp={loc_fp[:48]}"
        )
        return True
    return False


async def _attach_via_file_upload(
    page: Any,
    editor: Any,
    paths: list[str],
    *,
    fname_hint: str = "",
) -> tuple[bool, bool]:
    """로컬 PNG → composer ``input[type=file]`` / filechooser 업로드.

    반환: ``(업로드 시도 성공, 첨부 확인됨)``.
    """
    fname = fname_hint or (Path(paths[0]).name if paths else "?")
    abs_paths = [str(Path(p).resolve()) for p in paths]
    img_path = next(
        (
            p
            for p in abs_paths
            if Path(p).suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")
        ),
        None,
    )
    if not img_path:
        return False, False
    if fname and Path(img_path).name != fname:
        _tab_log(
            f"첨부: 파일 불일치 · 기대={fname} · "
            f"실제={Path(img_path).name}"
        )
        return False, False
    before = await _composer_pending_thumb_count(page, editor)
    before_score = await _composer_attachment_score(
        page, editor, fname_hint=fname
    )
    if before > 0 or await _composer_has_pending_attachment(
        page, editor, fname_hint=fname
    ):
        _tab_log(f"첨부: upload 보류 — 기존 썸네일 {before}장")
        return False, False
    _FC_ALLOW["v"] = True
    try:
        page_sc = _score_ai_image_url(page.url or "")
        upload_fns: list[tuple[str, Any]] = [
            (
                "inject",
                lambda: _inject_composer_file_input(
                    page, editor, abs_paths
                ),
            ),
            (
                "composer-input",
                lambda: _set_files_on_composer_input(
                    page, editor, abs_paths
                ),
            ),
            (
                "add-entry",
                lambda: _click_genspark_add_entry(
                    page,
                    abs_paths,
                    prefer_followup=page_sc >= 40,
                    allow_menu_hidden=False,
                ),
            ),
        ]
        for name, fn in upload_fns:
            _tab_log(f"첨부: upload 시도 {name} · {fname}")
            try:
                hit = await fn()
            except Exception as ex:
                _tab_log(f"첨부: upload {name} 예외 {ex}")
                continue
            if not hit:
                continue
            await page.wait_for_timeout(600)
            after, ok = await _wait_attach_verified(
                page,
                editor,
                before_count=before,
                before_score=before_score,
                fname=fname,
                max_wait_sec=2.0,
                poll_ms=500,
            )
            if not ok:
                await page.wait_for_timeout(500)
                after, ok = await _wait_attach_verified(
                    page,
                    editor,
                    before_count=before,
                    before_score=before_score,
                    fname=fname,
                    max_wait_sec=1.5,
                    poll_ms=500,
                )
            _tab_log(
                f"첨부: upload {name} · {Path(img_path).name} · "
                f"count={after} · verified={ok}"
            )
            if ok:
                return True, True
            return True, False
        return False, False
    finally:
        _FC_ALLOW["v"] = False


async def _focus_composer_editor(page: Any, editor: Any) -> None:
    """이어쓰기 입력란에 포커스 — Ctrl+V 붙여넣기용."""
    try:
        await page.bring_to_front()
    except Exception:
        pass
    try:
        await editor.scroll_into_view_if_needed(timeout=4000)
    except Exception:
        pass
    try:
        await editor.evaluate(
            """(el) => {
              if (!el) return;
              try { el.scrollIntoView({block: 'nearest', inline: 'nearest'}); } catch (e) {}
              try { el.focus(); } catch (e) {}
              try { el.click(); } catch (e) {}
            }"""
        )
    except Exception:
        pass
    try:
        box = await editor.bounding_box()
        if box and box.get("width", 0) > 40:
            x = float(box["x"]) + float(box["width"]) * 0.5
            y = float(box["y"]) + float(box["height"]) * 0.5
            await page.mouse.click(x, y)
    except Exception:
        try:
            await editor.click(timeout=3000)
        except Exception:
            pass
    await page.wait_for_timeout(350)


def _clear_clipboard_win() -> None:
    """OS 클립보드 비우기 — 이미지 재붙여넣기 방지."""
    if sys.platform != "win32":
        return
    try:
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                "Set-Clipboard -Value ''",
            ],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
            creationflags=_subprocess_no_window_flags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


async def _attach_via_clipboard_paste(
    page: Any,
    editor: Any,
    paths: list[str],
    *,
    fname_hint: str = "",
    at_end: bool = False,
    verify: bool = True,
) -> bool:
    """로컬 PNG → OS 클립보드 → composer 입력란 ``Ctrl+V``.

    ``at_end=True`` — 입력된 명령 끝(``Control+End``)에 1회 붙여넣기.
    ``verify=False`` — 붙여넣기 직후 썸네일 확인 생략 (즉시 전송용).
    """
    if sys.platform != "win32":
        _tab_log("첨부: Ctrl+V — Windows 전용")
        return False
    img_path = next(
        (
            p
            for p in paths
            if Path(p).suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")
        ),
        None,
    )
    if not img_path:
        return False
    fname = fname_hint or Path(img_path).name
    abs_path = str(Path(img_path).resolve())
    if fname and Path(abs_path).name != fname:
        _tab_log(
            f"첨부: 파일 불일치 · 기대={fname} · "
            f"실제={Path(abs_path).name}"
        )
        return False
    if not _copy_image_to_clipboard_win(abs_path):
        _tab_log(f"첨부: Ctrl+V 클립보드 복사 실패 · {Path(abs_path).name}")
        return False
    try:
        fsize = Path(abs_path).stat().st_size
    except OSError:
        fsize = 0
    _tab_log(
        f"첨부: 클립보드 복사 OK · {Path(abs_path).name} · {fsize} bytes"
    )
    try:
        before = await _composer_pending_thumb_count(page, editor)
        before_score = await _composer_attachment_score(
            page, editor, fname_hint=fname
        )
        if before > 0 or await _composer_has_pending_attachment(
            page, editor, fname_hint=fname
        ):
            _tab_log(f"첨부: Ctrl+V 보류 — 기존 썸네일 {before}장")
            return False
        await _focus_composer_editor(page, editor)
        if at_end:
            await page.keyboard.press("Control+End")
            await page.wait_for_timeout(80)
        await page.keyboard.press("Control+V")
        _clear_clipboard_win()
        if at_end:
            if not verify:
                _tab_log(f"첨부: Ctrl+V(명령끝) · {Path(img_path).name}")
                return True
            after, ok = await _wait_attach_verified(
                page,
                editor,
                before_count=before,
                before_score=before_score,
                fname=fname,
                max_wait_sec=2.5,
                poll_ms=250,
            )
            if not ok:
                await page.wait_for_timeout(400)
                after, ok = await _wait_attach_verified(
                    page,
                    editor,
                    before_count=before,
                    before_score=before_score,
                    fname=fname,
                    max_wait_sec=2.0,
                    poll_ms=250,
                )
            _tab_log(
                f"첨부: Ctrl+V(명령끝) · {Path(img_path).name} · "
                f"count={after} · verified={ok}"
            )
            return ok
        deadline = time.time() + 8.0
        while time.time() < deadline:
            await page.wait_for_timeout(350)
            after, ok = await _paste_attach_verified(
                page,
                editor,
                before_count=before,
                before_score=before_score,
                fname_hint=fname,
            )
            if ok and after == 1:
                _tab_log(
                    f"첨부: Ctrl+V OK · {Path(img_path).name} · 1장"
                )
                return True
            if after > 1:
                _tab_log(
                    f"첨부: Ctrl+V 중복 {after}장 · {Path(img_path).name}"
                )
                return False
        after, ok = await _paste_attach_verified(
            page,
            editor,
            before_count=before,
            before_score=before_score,
            fname_hint=fname,
        )
        if ok:
            _tab_log(
                f"첨부: Ctrl+V OK(늦음) · {Path(img_path).name} · "
                f"{after}장"
            )
            return True
        _tab_log(
            f"첨부: Ctrl+V 미확인 · {Path(img_path).name} · "
            f"썸네일={after}"
        )
        return False
    except Exception as ex:
        _tab_log(f"첨부: Ctrl+V 실패 {ex}")
        return False


def _all_file_inputs_js() -> str:
    """light + shadow DOM file input 수집."""
    return """
      const out = [];
      const seen = new Set();
      const walk = (root) => {
        if (!root || seen.has(root)) return;
        seen.add(root);
        try {
          root.querySelectorAll('input[type=file]').forEach((inp) => out.push(inp));
        } catch (e) {}
        try {
          for (const el of root.querySelectorAll('*')) {
            if (el.shadowRoot) walk(el.shadowRoot);
          }
        } catch (e) {}
      };
      walk(document);
      return out;
    """


async def _composer_file_input_diag(
    page: Any, editor: Any, *, after_menu: bool = False
) -> dict[str, Any]:
    """file input 후보 개수·최고 점수 (로그용)."""
    min_score = 0 if after_menu else 20
    result = await page.evaluate(
        f"""(args) => {{
          const [ed, afterMenu, minScore] = args;
          const inputs = (() => {{
            {_all_file_inputs_js()}
          }})();
          if (!ed) return {{ n: inputs.length, best: 0 }};
          const vh = window.innerHeight || 800;
          const er = ed.getBoundingClientRect();
          let bestScore = -1e9;
          for (const inp of inputs) {{
            if (inp.disabled) continue;
            const bad = inp.closest(
              '[class*="canvas" i],[class*="edit-tool" i],'
              + '[class*="brush" i],[class*="magic" i],[class*="inpaint" i]'
            );
            if (bad) continue;
            let score = 0;
            const menu = inp.closest(
              '[role="menu"],[role="dialog"],[class*="popover" i],'
              + '[class*="dropdown" i],[class*="menu" i]'
            );
            if (menu) {{
              const mr = menu.getBoundingClientRect();
              if (mr.height > 8 && mr.width > 8) score += 55;
            }}
            const root = inp.closest(
              '[class*="composer" i],[class*="chat-input" i],'
              + '[class*="prompt" i],[class*="add-entry" i],footer,form,aside'
            );
            if (root) score += 30;
            const accept = (inp.getAttribute('accept') || '').toLowerCase();
            if (/image|png|jpg|jpeg|webp|\\*/.test(accept)) score += 12;
            const r = inp.getBoundingClientRect();
            if (afterMenu && r.height === 0 && r.width === 0) score += 40;
            if (r.bottom >= vh * 0.32) score += 20;
            if (Math.abs(r.bottom - er.bottom) < 140) score += 40;
            if (r.right <= er.left + 40) score += 15;
            if (score > bestScore) bestScore = score;
          }}
          return {{ n: inputs.length, best: bestScore, ok: bestScore >= minScore }};
        }}""",
        [editor, after_menu, min_score],
    )
    return result if isinstance(result, dict) else {}


async def _pick_composer_file_input_handle(
    page: Any, editor: Any, *, after_menu: bool = False
) -> tuple[Any | None, dict[str, Any]]:
    """composer·메뉴 근처 file input element handle."""
    min_score = 0 if after_menu else 20
    handle = await page.evaluate_handle(
        f"""(args) => {{
          const [ed, afterMenu, minScore] = args;
          const inputs = (() => {{
            {_all_file_inputs_js()}
          }})();
          if (!ed) return null;
          const vh = window.innerHeight || 800;
          const er = ed.getBoundingClientRect();
          let best = null, bestScore = -1e9;
          for (const inp of inputs) {{
            if (inp.disabled) continue;
            const bad = inp.closest(
              '[class*="canvas" i],[class*="edit-tool" i],'
              + '[class*="brush" i],[class*="magic" i],[class*="inpaint" i]'
            );
            if (bad) continue;
            let score = 0;
            const menu = inp.closest(
              '[role="menu"],[role="dialog"],[class*="popover" i],'
              + '[class*="dropdown" i],[class*="menu" i]'
            );
            if (menu) {{
              const mr = menu.getBoundingClientRect();
              if (mr.height > 8 && mr.width > 8) score += 55;
            }}
            const root = inp.closest(
              '[class*="composer" i],[class*="chat-input" i],'
              + '[class*="prompt" i],[class*="add-entry" i],footer,form,aside'
            );
            if (root) score += 30;
            const accept = (inp.getAttribute('accept') || '').toLowerCase();
            if (/image|png|jpg|jpeg|webp|\\*/.test(accept)) score += 12;
            const r = inp.getBoundingClientRect();
            if (afterMenu && r.height === 0 && r.width === 0) score += 40;
            if (r.bottom >= vh * 0.32) score += 20;
            if (Math.abs(r.bottom - er.bottom) < 140) score += 40;
            if (r.right <= er.left + 40) score += 15;
            if (score > bestScore) {{ bestScore = score; best = inp; }}
          }}
          if (!best || bestScore < minScore) return null;
          return best;
        }}""",
        [editor, after_menu, min_score],
    )
    element = handle.as_element()
    meta = await _composer_file_input_diag(
        page, editor, after_menu=after_menu
    )
    return element, meta


async def _inject_composer_file_input(
    page: Any, editor: Any, paths: list[str]
) -> bool:
    """composer 근처 hidden ``input[type=file]`` 주입 → ``set_files``."""
    fname = Path(paths[0]).name if paths else "?"
    try:
        handle = await page.evaluate_handle(
            """(ed) => {
              if (!ed) return null;
              const root = ed.closest(
                '[class*="composer" i],[class*="chat-input" i],footer,form,aside'
              ) || ed.parentElement || document.body;
              let inp = root.querySelector('input[data-wisdom-ref-file]');
              if (!inp) {
                inp = document.createElement('input');
                inp.type = 'file';
                inp.accept = 'image/png,image/jpeg,image/webp';
                inp.setAttribute('data-wisdom-ref-file', '1');
                inp.style.cssText =
                  'position:fixed;left:-9999px;width:1px;height:1px;opacity:0;';
                root.appendChild(inp);
              }
              return inp;
            }""",
            editor,
        )
        element = handle.as_element()
        if element is None:
            return False
        await element.set_input_files(paths, timeout=12_000)
        await page.evaluate(
            """(inp) => {
              if (!inp) return;
              inp.dispatchEvent(new Event('input', {bubbles: true}));
              inp.dispatchEvent(new Event('change', {bubbles: true}));
            }""",
            element,
        )
        await page.wait_for_timeout(800)
        _tab_log(f"첨부: inject input[file] OK · {fname}")
        return True
    except Exception as ex:
        _tab_log(f"첨부: inject input[file] 실패 {ex}")
        return False


async def _set_files_on_composer_input(
    page: Any,
    editor: Any,
    paths: list[str],
    *,
    after_menu: bool = False,
) -> bool:
    """composer·메뉴 ``input[type=file]`` 에 직접 주입."""
    try:
        element, meta = await _pick_composer_file_input_handle(
            page, editor, after_menu=after_menu
        )
        n_inp = int(meta.get("n") or 0)
        best_sc = meta.get("best")
        if element is None:
            _tab_log(
                f"첨부: composer input 없음 n={n_inp} "
                f"best={best_sc} menu={after_menu}"
            )
            return False
        await element.set_input_files(paths, timeout=12_000)
        await page.wait_for_timeout(700)
        _tab_log(
            f"첨부: composer input[file] (component) "
            f"n={n_inp} best={best_sc} menu={after_menu}"
        )
        return True
    except Exception as ex:
        _tab_log(f"첨부: composer input 실패 {ex}")
        return False


async def _set_files_on_local_menu_input(page: Any, paths: list[str]) -> bool:
    """「로컬 파일 찾기」 menuitem 내부 hidden input."""
    for sel in (
        "[role='menuitem']:has-text('로컬 파일 찾기') input[type=file]",
        "li:has-text('로컬 파일 찾기') input[type=file]",
        "button:has-text('로컬 파일 찾기') input[type=file]",
        "label:has-text('로컬 파일 찾기') input[type=file]",
        "[role='menu'] input[type=file]",
    ):
        loc = page.locator(sel).first
        try:
            if await loc.count() == 0:
                continue
            await loc.set_input_files(paths, timeout=12_000)
            await page.wait_for_timeout(600)
            _tab_log(f"첨부: menu input[file] (component) sel={sel[:50]}")
            return True
        except Exception:
            continue
    try:
        handle = await page.evaluate_handle(
            """() => {
              const pick = (el) => {
                if (!el) return null;
                const inp = el.querySelector('input[type=file]');
                if (inp) return inp;
                const forId = el.getAttribute('for') || el.htmlFor;
                if (forId) {
                  const t = document.getElementById(forId);
                  if (t && t.type === 'file') return t;
                }
                return null;
              };
              for (const el of document.querySelectorAll(
                '[role=menuitem], li, button, label, a, div, span'
              )) {
                const txt = ((el.innerText || el.textContent) || '').trim();
                if (!/로컬\\s*파일\\s*찾기/i.test(txt)) continue;
                const inp = pick(el) || pick(el.closest('label'));
                if (inp) return inp;
              }
              return null;
            }"""
        )
        element = handle.as_element()
        if element is not None:
            await element.set_input_files(paths, timeout=12_000)
            await page.wait_for_timeout(600)
            _tab_log("첨부: menu input[file] (evaluate)")
            return True
    except Exception:
        pass
    return False


async def _click_composer_plus(page: Any, editor: Any) -> bool:
    """이어쓰기 composer 왼쪽 ``+`` (locator → evaluate → 좌표)."""
    plus = await _find_add_entry_btn(page, editor)
    if plus is not None:
        try:
            await plus.click(timeout=3000)
            _tab_log("첨부: + 클릭 (locator)")
            return True
        except Exception:
            pass
    try:
        clicked = bool(
            await editor.evaluate(
                """(ed) => {
                  if (!ed) return false;
                  const er = ed.getBoundingClientRect();
                  let best = null, bestScore = -1e9;
                  for (const node of document.querySelectorAll(
                    'button,[role=button],svg.add-entry-icon,.add-entry-icon,'
                    + '[class*="add-entry" i],div[class*="add" i]'
                  )) {
                    const btn = node.closest(
                      'button,[role=button],[class*="add-entry" i]'
                    ) || node;
                    const r = btn.getBoundingClientRect();
                    if (r.width < 16 || r.width > 80 || r.height < 16) continue;
                    if (Math.abs(r.bottom - er.bottom) > 96) continue;
                    if (r.right > er.left + 32) continue;
                    if (r.left < er.left - 280) continue;
                    const score = -Math.abs(r.bottom - er.bottom)
                      - Math.abs(r.right - er.left) * 0.15;
                    if (score > bestScore) { bestScore = score; best = btn; }
                  }
                  if (!best) return false;
                  best.click();
                  return true;
                }"""
            )
        )
        if clicked:
            _tab_log("첨부: + 클릭 (evaluate)")
            return True
    except Exception:
        pass
    try:
        box = await editor.bounding_box()
        if box and box.get("width", 0) > 40:
            x = float(box["x"]) - 34.0
            y = float(box["y"]) + float(box["height"]) * 0.5
            await page.mouse.click(x, y)
            _tab_log(f"첨부: + 클릭 (좌표 x={x:.0f})")
            return True
    except Exception as ex:
        _tab_log(f"첨부: + 클릭 실패 {ex}")
    return False


async def _find_add_entry_btn(page: Any, editor: Any) -> Any | None:
    """이어쓰기 composer 옆 ``+`` 버튼."""
    for sel in (
        "svg.add-entry-icon",
        ".add-entry-icon",
        "button:has(svg.add-entry-icon)",
        "[class*='add-entry' i]",
    ):
        loc = page.locator(sel)
        try:
            n = await loc.count()
        except Exception:
            n = 0
        for i in range(min(n, 8)):
            item = loc.nth(i)
            try:
                if not await item.is_visible(timeout=300):
                    continue
                btn = item.locator(
                    "xpath=ancestor-or-self::button[1] | "
                    "ancestor-or-self::*[@role='button'][1]"
                ).first
                target = btn if await btn.count() > 0 else item
                if await _add_entry_near_editor(target, editor):
                    return target
                if await _add_entry_near_editor(
                    target, editor, relaxed=True
                ):
                    return target
            except Exception:
                continue
    return None


async def _attach_in_conversation(
    page: Any,
    paths: list[str],
    *,
    context: Any | None = None,
    work_url: str = "",
    at_end: bool = False,
    submit_after: bool = False,
    skip_pre_idle: bool = False,
) -> bool:
    """같은 ``agents?id=`` 탭 — ``Ctrl+V`` 로 직전 PNG 첨부 (실패 시 upload 폴백)."""
    page = await _ensure_attach_page(context, page, work_url)
    _ATTACH_KEEP_PAGE["page"] = page
    _ATTACH_WORK_URL["v"] = (work_url or page.url or "").strip()
    fname = Path(paths[0]).name if paths else "?"
    try:
        if not skip_pre_idle and not await _wait_until_generation_idle(
            page, timeout_sec=120.0, stable_hits=3, label=f"첨부전 {fname}"
        ):
            _tab_log("첨부: 생성 중 — Ctrl+V 생략")
            return False
        await _scroll_to_composer(page)
        await _wait_editor_ready(page, timeout_sec=45.0)
        await _raise_if_page_limited(page, label=f"첨부 {fname}")
        page_sc = _score_ai_image_url(page.url or "")
        if page_sc < 40:
            cands = await _prompt_editor_candidates(
                page, prefer_followup=False
            )
        else:
            cands = await _prompt_editor_candidates(
                page, prefer_followup=True
            )
            if not cands:
                cands = await _prompt_editor_candidates(
                    page, prefer_followup=False
                )
        if not cands:
            _tab_log("첨부: composer 없음")
            return False
        editor = cands[0]
        remain = await _ensure_composer_attachments_clear(
            page, editor, label=fname
        )
        if remain > 0:
            _tab_log(f"첨부: 기존 썸네일 {remain}장 제거 실패 · {fname}")
            return False
        pos = "명령끝" if at_end else "입력란"
        landing_fast_submit = submit_after and page_sc < 40
        instant_submit = submit_after
        if landing_fast_submit:
            _tab_log(
                f"첨부: Ctrl+V 1회({pos}) · {fname} · "
                "랜딩(로컬파일 메뉴 생략·직후 전송)"
            )
        elif instant_submit:
            _tab_log(
                f"첨부: Ctrl+V 1회({pos}) · {fname} · 직후 전송"
            )
        else:
            _tab_log(f"첨부: Ctrl+V 1회({pos}) · {fname}")
        verified = await _attach_via_clipboard_paste(
            page,
            editor,
            paths,
            fname_hint=fname,
            at_end=at_end,
            verify=True,
        )
        if not verified:
            await page.wait_for_timeout(600)
            if await _composer_paste_chip_visible(
                page, editor, fname_hint=fname
            ):
                verified = True
                _tab_log(f"첨부: Ctrl+V pasted-text 칩 확인 · {fname}")
        if not verified and instant_submit:
            ctx = "랜딩" if landing_fast_submit else "대화"
            _tab_log(
                f"첨부: Ctrl+V 미확인 — upload 생략·직후 전송 · {fname} · {ctx}"
            )
            verified = True
        elif not verified:
            _tab_log(f"첨부: Ctrl+V 미확인 — upload 폴백 · {fname}")
            upload_ok, verified = await _attach_via_file_upload(
                page, editor, paths, fname_hint=fname
            )
            if not upload_ok:
                if await _composer_paste_chip_visible(
                    page, editor, fname_hint=fname
                ):
                    verified = True
                    _tab_log(
                        f"첨부: upload 실패·칩 확인 — 전송 진행 · {fname}"
                    )
                else:
                    remain = await _composer_pending_thumb_count(page, editor)
                    if remain > 0 or await _composer_has_pending_attachment(
                        page, editor, fname_hint=fname
                    ):
                        await _ensure_composer_attachments_clear(
                            page, editor, label=fname
                        )
                    return False
        if not verified:
            _tab_log(
                f"첨부: 미확인 · {fname} — 전송 보류"
            )
            await _ensure_composer_attachments_clear(
                page, editor, label=fname
            )
            return False
        if submit_after:
            _tab_log(
                f"첨부: Ctrl+V 후 1초 뒤 전송 · {fname} · "
                f"{_page_ctx_label(page.url or '')}"
            )
            await _submit_after_attach(page, editor)
            _tab_log(
                f"첨부·전송 완료 · {fname} · "
                f"{_page_ctx_label(page.url or '')}"
            )
            return True
        _tab_log(
            f"첨부: 대화탭 Ctrl+V OK · {fname} · "
            f"{_page_ctx_label(page.url or '')}"
        )
        return True
    except Exception as ex:
        _tab_log(f"첨부: 대화탭 실패 {ex}")
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        return False
    finally:
        if context is not None:
            try:
                keep = _ATTACH_KEEP_PAGE.get("page") or page
                await _close_all_extra_tabs(context, keep)
                page = await _ensure_attach_page(context, keep, work_url)
                await page.bring_to_front()
                await _install_page_guards(page)
            except Exception:
                pass
        _ATTACH_KEEP_PAGE["page"] = None
        _ATTACH_WORK_URL["v"] = ""


def _strict_ref_path_ok(
    attach_ref_path: Path,
    srt_sec: int,
    *,
    interval_sec: int,
    scene_secs: list[int] | None = None,
) -> bool:
    """붙여넣을 파일이 허용된 직전 참조 슬롯인지."""
    return reference_png_path_ok(
        attach_ref_path,
        srt_sec,
        interval_sec=interval_sec,
        scene_secs=scene_secs,
    )


async def _attach_reference_after_command(
    page: Any,
    context: Any,
    attach_ref_path: Path,
    *,
    url: str,
    work_url: str,
    srt_sec: int,
    watch_prior: int,
    ref_follow: bool,
    interval_sec: int = 20,
    submit_after: bool = True,
    png_dir: Path | None = None,
    scene_secs: list[int] | None = None,
) -> tuple[bool, Any]:
    """명령 입력 **직후** — composer 명령 끝 ``Ctrl+V`` → (옵션) 즉시 전송."""
    slot = previous_reference_slot_sec(
        srt_sec, interval_sec=interval_sec, scene_secs=scene_secs
    )
    expect = attach_ref_path.name
    if not _strict_ref_path_ok(
        attach_ref_path,
        srt_sec,
        interval_sec=interval_sec,
        scene_secs=scene_secs,
    ):
        fixed: Path | None = None
        if png_dir is not None:
            _slot2, fixed = resolve_strict_reference_png(
                png_dir,
                srt_sec,
                interval_sec=interval_sec,
                last_completed_sec=_LAST_SCENE_REF.get("sec"),
                last_completed_path=_LAST_SCENE_REF.get("path"),
                scene_secs=scene_secs,
            )
            if _slot2 is not None:
                slot = _slot2
        if fixed is not None and _strict_ref_path_ok(
            fixed,
            srt_sec,
            interval_sec=interval_sec,
            scene_secs=scene_secs,
        ):
            _tab_log(
                f"참조 경로 보정 SRT_{srt_sec:03d} · "
                f"{attach_ref_path.name} → {fixed.name}"
            )
            attach_ref_path = fixed
        else:
            _tab_log(
                f"참조 파일 불일치 SRT_{srt_sec:03d} · "
                f"기대={srt_png_name(slot) if slot is not None else '?'} · "
                f"실제={attach_ref_path.name}"
            )
            return False, page
    try:
        ref_bytes = attach_ref_path.stat().st_size
    except OSError:
        ref_bytes = 0
    _tab_log(
        f"참조 첨부 SRT_{srt_sec:03d} · 슬롯={attach_ref_path.stem} · "
        f"file={attach_ref_path.name} · {ref_bytes} bytes · "
        f"{attach_ref_path.resolve()}"
    )
    t_attach = time.perf_counter()
    ref_attached = False
    page = await _ensure_attach_page(context, page, work_url)
    if await _page_is_magic_redraw(page):
        page, _ = await _soft_exit_magic_redraw(page)
    if not await _wait_until_generation_idle(
        page,
        timeout_sec=120.0,
        stable_hits=3,
        label=f"명령끝첨부 SRT_{srt_sec:03d}",
        watch_srt_sec=int(watch_prior) if watch_prior else None,
    ):
        _tab_log(f"명령끝첨부 생략 SRT_{srt_sec:03d} — 생성 중")
        return False, page
    await _scroll_to_composer(page)
    page = await _ensure_attach_page(context, page, work_url)
    page_sc = _score_ai_image_url(page.url or "")
    if page_sc < 40:
        cands = await _prompt_editor_candidates(page, prefer_followup=False)
    else:
        cands = await _prompt_editor_candidates(page, prefer_followup=True)
        if not cands:
            cands = await _prompt_editor_candidates(
                page, prefer_followup=False
            )
    if cands:
        await _ensure_composer_attachments_clear(
            page, cands[0], label=attach_ref_path.name
        )
    ref_attached = await _attach_in_conversation(
        page,
        [str(attach_ref_path.resolve())],
        context=context,
        work_url=work_url or "",
        at_end=True,
        submit_after=submit_after,
        skip_pre_idle=True,
    )
    _timing_log(
        f"명령끝참조 SRT_{srt_sec:03d}",
        t_attach,
        extra=(
            f"ok={ref_attached} · {attach_ref_path.name} · "
            f"{_page_ctx_label(page.url or '')}"
        ),
    )
    if ref_attached:
        _tab_log(
            f"명령끝 첨부 확인 SRT_{srt_sec:03d} · "
            f"{attach_ref_path.name}"
        )
    return ref_attached, page


async def _resubmit_scene_after_phantom(
    page: Any,
    context: Any,
    *,
    srt_sec: int,
    send_prompt: str,
    attach_ref_path: Path | None,
    url: str,
    work_url: str,
    watch_prior: int,
    interval_sec: int,
    on_landing_composer: bool,
    resubmit_i: int,
    png_dir: Path | None = None,
    scene_secs: list[int] | None = None,
) -> tuple[bool, Any, bool]:
    """성공 문구만(이미지 없음) — composer 재입력·참조 재첨부·재전송."""
    page = await _alive_page(context, page)
    page_sc = _score_ai_image_url(page.url or "")
    on_landing = page_sc < 40
    _tab_log(
        f"성공문구만 — 재첨부·재전송 {resubmit_i}/{PHANTOM_RESUBMIT_MAX} "
        f"SRT_{srt_sec:03d} · {_page_ctx_label(page.url or '')}"
    )
    await _scroll_to_composer(page)
    if not await _wait_until_generation_idle(
        page,
        timeout_sec=90.0,
        stable_hits=2,
        label=f"재전송전 SRT_{srt_sec:03d}",
        watch_srt_sec=srt_sec,
    ):
        _tab_log(f"재전송 보류 SRT_{srt_sec:03d} — 아직 생성 중")
        return False, page, False
    cands = await _prompt_editor_candidates(
        page,
        prefer_followup=not on_landing and not on_landing_composer,
    )
    if not cands:
        cands = await _prompt_editor_candidates(
            page, prefer_followup=False
        )
    if not cands:
        _tab_log(f"재전송 실패 SRT_{srt_sec:03d} — composer 없음")
        return False, page, False
    stale = await _ensure_composer_attachments_clear(
        page, cands[0], label=f"재전송 SRT_{srt_sec:03d}"
    )
    if stale > 0:
        _tab_log(
            f"재전송 보류 SRT_{srt_sec:03d} — composer 잔여 첨부 {stale}장"
        )
        return False, page, False
    if not send_prompt.strip():
        _tab_log(f"재전송 실패 SRT_{srt_sec:03d} — 명령 텍스트 없음")
        return False, page, False
    if not await _fill_first_editable(
        page,
        send_prompt,
        prefer_followup=not on_landing and not on_landing_composer,
        skip_ready_wait=True,
        after_image_attach=False,
    ):
        _tab_log(f"재전송 실패 SRT_{srt_sec:03d} — 입력란 없음")
        return False, page, False
    ref_attached = False
    if attach_ref_path is not None and attach_ref_path.is_file():
        ref_follow = page_sc >= 40 or "/agents" in (page.url or "").lower()
        ref_attached, page = await _attach_reference_after_command(
            page,
            context,
            attach_ref_path,
            url=url,
            work_url=work_url or "",
            srt_sec=srt_sec,
            watch_prior=int(watch_prior),
            ref_follow=ref_follow,
            interval_sec=interval_sec,
            submit_after=True,
            png_dir=png_dir,
            scene_secs=scene_secs,
        )
        if not ref_attached:
            _tab_log(
                f"재전송 실패 SRT_{srt_sec:03d} — 참조 재첨부·전송 실패"
            )
            return False, page, False
    else:
        await _ensure_submitted(page)
    _tab_log(f"재전송 제출 SRT_{srt_sec:03d} · ref={ref_attached}")
    return True, page, ref_attached


async def _click_local_file_menu(page: Any, paths: list[str]) -> bool:
    """``+`` 메뉴의 「로컬 파일 찾기」 → filechooser (폴백)."""
    try:
        async with page.expect_file_chooser(timeout=10_000) as fc_info:
            if not await _click_local_file_menu_item(page):
                return False
        chooser = await fc_info.value
        await chooser.set_files(paths)
        await asyncio.sleep(0.6)
        _tab_log("첨부: 메뉴 로컬파일 → filechooser")
        return True
    except Exception:
        return False


async def _click_genspark_add_entry(
    page: Any,
    paths: list[str],
    *,
    prefer_followup: bool = False,
    allow_menu_hidden: bool = True,
) -> bool:
    """입력창 왼쪽 ``+`` (add-entry-icon / 파일 및 기타 추가) → filechooser.

    ``prefer_followup=True`` 이면 **이어쓰기** 입력창 옆 ``+`` 만 클릭한다.
    ``allow_menu_hidden=False`` 이면 메뉴 뒤 hidden input 폴백 금지 (편집 UI 회피).
    """
    follow_editor = None
    if prefer_followup:
        cands = await _prompt_editor_candidates(page, prefer_followup=True)
        if cands:
            follow_editor = cands[0]
            try:
                await follow_editor.click(timeout=2000)
                await page.wait_for_timeout(200)
            except Exception:
                pass
            _tab_log("첨부: 이어쓰기 입력창(composer) 범위")
    # 1) 아이콘·버튼 후보 (사용자가 확인한 Genspark UI)
    entry_sels = (
        "svg.add-entry-icon",
        ".add-entry-icon",
        "button:has(svg.add-entry-icon)",
        "[class*='add-entry' i]",
        "[aria-label*='파일 및 기타' i]",
        "[title*='파일 및 기타' i]",
        "[aria-label*='Add files' i]",
        "[title*='Add files' i]",
    )
    clicked = False
    for sel in entry_sels:
        loc = page.locator(sel)
        try:
            n = await loc.count()
        except Exception:
            n = 0
        for i in range(min(n, 6)):
            item = loc.nth(i)
            try:
                if not await item.is_visible(timeout=400):
                    continue
                # SVG 면 클릭 가능한 부모 버튼으로
                try:
                    btn = item.locator(
                        "xpath=ancestor-or-self::button[1] | ancestor-or-self::*[@role='button'][1]"
                    ).first
                    if await btn.count() > 0 and await btn.is_visible(timeout=200):
                        target_btn = btn
                    else:
                        target_btn = item
                except Exception:
                    target_btn = item
                near_ok = await _add_entry_near_editor(
                    target_btn, follow_editor
                )
                if not near_ok and follow_editor is not None:
                    near_ok = await _add_entry_near_editor(
                        target_btn, follow_editor, relaxed=True
                    )
                if follow_editor is not None and not near_ok:
                    continue
                try:
                    async with page.expect_file_chooser(timeout=2500) as fc_info:
                        await target_btn.click(timeout=3000)
                    chooser = await fc_info.value
                    await chooser.set_files(paths)
                    await asyncio.sleep(0.6)
                    _tab_log(
                        "첨부: add-entry → filechooser 직행"
                        + (" · followup" if follow_editor else "")
                    )
                    return True
                except Exception:
                    await target_btn.click(timeout=3000)
                    clicked = True
                    _tab_log(
                        f"첨부: add-entry 클릭 (메뉴 대기) sel={sel}"
                        + (" · followup" if follow_editor else "")
                    )
                    break
            except Exception:
                continue
        if clicked:
            break

    if not clicked:
        # 좌표: 입력창 왼쪽 원형 +
        try:
            use_follow = bool(prefer_followup)
            hit = await page.evaluate(
                """(useFollow) => {
                  const vh = window.innerHeight || 800;
                  let editor = null, best = 0;
                  const isNewPh = (ph) => /new\\s*image|create\\s*(an?\\s*)?image|새\\s*이미지|새\\s*대화|new\\s*chat/.test(ph || '');
                  for (const el of document.querySelectorAll(
                    "textarea, [contenteditable='true'], [role='textbox']"
                  )) {
                    const r = el.getBoundingClientRect();
                    if (r.width < 80 || r.height < 20) continue;
                    const ph = (
                      (el.getAttribute('placeholder') || '') + ' '
                      + (el.getAttribute('aria-label') || '')
                    ).toLowerCase();
                    if (useFollow && isNewPh(ph)) continue;
                    if (useFollow && r.bottom < vh * 0.45) continue;
                    if (!useFollow && r.bottom < vh * 0.35) continue;
                    if (r.bottom > best) { best = r.bottom; editor = el; }
                  }
                  if (!editor) return null;
                  const er = editor.getBoundingClientRect();
                  const nodes = document.querySelectorAll(
                    'button, [role="button"], svg.add-entry-icon, .add-entry-icon'
                  );
                  for (const el of nodes) {
                    const node = el.tagName === 'svg' || (el.classList && el.classList.contains('add-entry-icon'))
                      ? (el.closest('button,[role=button]') || el)
                      : el;
                    const r = node.getBoundingClientRect();
                    if (r.width < 18 || r.width > 56 || r.height < 18 || r.height > 56)
                      continue;
                    if (Math.abs(r.bottom - er.bottom) > 96) continue;
                    if (r.right > er.left + 88) continue;
                    if (r.left < er.left - 220) continue;
                    const t = (
                      (node.getAttribute('aria-label') || '') + ' '
                      + (node.getAttribute('title') || '')
                    );
                    const hasIcon = !!(node.querySelector
                      && (node.querySelector('svg.add-entry-icon')
                        || node.querySelector('.add-entry-icon')));
                    if (hasIcon || /파일|Add files|기타 추가/i.test(t))
                      return {x: r.x + r.width / 2, y: r.y + r.height / 2};
                  }
                  return null;
                }""",
                use_follow,
            )
            if hit and hit.get("x"):
                try:
                    async with page.expect_file_chooser(timeout=2500) as fc_info:
                        await page.mouse.click(float(hit["x"]), float(hit["y"]))
                    chooser = await fc_info.value
                    await chooser.set_files(paths)
                    await asyncio.sleep(0.6)
                    _tab_log("첨부: add-entry 좌표 → filechooser")
                    return True
                except Exception:
                    await page.mouse.click(float(hit["x"]), float(hit["y"]))
                    clicked = True
                    _tab_log("첨부: add-entry 좌표 클릭 (메뉴 대기)")
        except Exception:
            pass

    if not clicked:
        _tab_log("첨부: + 버튼 미발견")
        return False

    await page.wait_for_timeout(450)
    if await _click_local_file_menu(page, paths):
        return True
    # 2) 메뉴에서 파일/이미지 항목 (폴백)
    for text in (
        "로컬 파일 찾기",
        "로컬 파일",
        "Find local file",
        "파일",
        "이미지",
        "사진",
        "Upload",
        "File",
        "Image",
        "Photo",
        "컴퓨터에서",
        "내 기기",
        "Browse",
        "Upload file",
        "Add file",
    ):
        if re.search(r"video|동영상|영상", text, re.I):
            continue
        for sel in (
            f"[role='menuitem']:has-text('{text}')",
            f"[role='option']:has-text('{text}')",
            f"button:has-text('{text}')",
            f"[role='button']:has-text('{text}')",
            f"div:has-text('{text}')",
            f"li:has-text('{text}')",
        ):
            item = page.locator(sel).first
            try:
                if not await item.is_visible(timeout=400):
                    continue
                async with page.expect_file_chooser(timeout=8000) as fc_info:
                    await item.click(timeout=3000)
                chooser = await fc_info.value
                await chooser.set_files(paths)
                await asyncio.sleep(0.6)
                _tab_log(f"첨부: 메뉴 '{text}' → filechooser")
                return True
            except Exception:
                continue
    # 3) 메뉴 연 뒤 숨은 file input — 편집 UI 진입 원인, PNG 참조 시 금지
    if allow_menu_hidden:
        if await _try_set_files_on_target(page, paths):
            _tab_log("첨부: 메뉴 후 hidden input")
            return True
    else:
        _tab_log("첨부: 메뉴 후 hidden input 생략 (filechooser-only)")
    _tab_log("첨부: add-entry 클릭 후 filechooser 없음")
    return False


async def _try_filechooser_click(
    target: Any,
    paths: list[str],
    *,
    prefer_followup: bool = False,
    allow_menu_hidden: bool = True,
) -> bool:
    page = getattr(target, "page", target)
    # Genspark 입력창 왼쪽 + (파일 및 기타 추가) 우선
    if await _click_genspark_add_entry(
        page,
        paths,
        prefer_followup=prefer_followup,
        allow_menu_hidden=allow_menu_hidden,
    ):
        return True
    for text in (
        "Attach",
        "Upload",
        "첨부",
        "파일",
        "Add file",
        "업로드",
        "Add",
        "+",
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
                if not await btn.is_visible(timeout=500):
                    continue
                async with page.expect_file_chooser(timeout=10_000) as fc_info:
                    await btn.click(timeout=4000)
                chooser = await fc_info.value
                await chooser.set_files(paths)
                await asyncio.sleep(0.8)
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
        "[aria-label*='파일 및 기타' i]",
        "svg.add-entry-icon",
        ".add-entry-icon",
    ):
        btns = target.locator(sel)
        try:
            n = await btns.count()
        except Exception:
            n = 0
        for i in range(min(n, 16)):
            btn = btns.nth(i)
            try:
                if not await btn.is_visible(timeout=350):
                    continue
                async with page.expect_file_chooser(timeout=7000) as fc_info:
                    await btn.click(timeout=3000)
                chooser = await fc_info.value
                await chooser.set_files(paths)
                await asyncio.sleep(0.8)
                return True
            except Exception:
                continue
    return False


async def _attach_files(
    page: Any,
    files: list[Path],
    *,
    prefer_followup: bool = False,
    context: Any | None = None,
    allow_hidden_input: bool | None = None,
    work_url: str = "",
) -> bool:
    """input[type=file] / filechooser 로 파일 첨부.

    ``prefer_followup=True`` — 같은 대화 **이어쓰기** 입력창에만 첨부.
    ``allow_hidden_input=False`` — ``+``/filechooser 만 (편집 UI hidden input 회피).
    """
    paths = [str(Path(p).resolve()) for p in files if Path(p).is_file()]
    if not paths:
        return False
    page_sc = _score_ai_image_url(page.url or "")
    if allow_hidden_input is None:
        allow_hidden_input = page_sc >= 40 and not prefer_followup
    if prefer_followup and page_sc >= 40:
        if await _attach_in_conversation(
            page, paths, context=context, work_url=work_url
        ):
            return True
        page = await _ensure_attach_page(context, page, work_url)
        cands = await _prompt_editor_candidates(page, prefer_followup=True)
        if cands and await _composer_has_pending_attachment(page, cands[0]):
            _tab_log("첨부: 대화탭 이미 첨부됨 — + 재시도 생략")
            return True
        # agents 대화탭: +/filechooser 반복 루프 금지 (이중 첨부·지연 방지)
        return False
    _FC_ALLOW["v"] = True
    _ATTACH_KEEP_PAGE["page"] = page
    _ATTACH_WORK_URL["v"] = (work_url or page.url or "").strip()
    try:
        try:
            await page.bring_to_front()
        except Exception:
            pass
        if prefer_followup or page_sc >= 40:
            await _wait_until_generation_idle(
                page, timeout_sec=120.0, stable_hits=2, label="첨부폴백"
            )
        max_attempts = 3 if prefer_followup else 8
        for _attempt in range(max_attempts):
            if allow_hidden_input:
                for target in _iter_page_targets(page):
                    if await _try_set_files_on_target(target, paths):
                        _tab_log(
                            "첨부: hidden input"
                            + (" · followup" if prefer_followup else "")
                        )
                        return True
            for target in _iter_page_targets(page):
                if await _try_filechooser_click(
                    target,
                    paths,
                    prefer_followup=prefer_followup,
                    allow_menu_hidden=allow_hidden_input,
                ):
                    return True
            try:
                if prefer_followup:
                    cands = await _prompt_editor_candidates(
                        page, prefer_followup=True
                    )
                    if cands:
                        await cands[0].click(timeout=2000)
                    else:
                        await page.locator(
                            "textarea:visible, [contenteditable='true']:visible"
                        ).last.click(timeout=2000)
                else:
                    await page.locator(
                        "textarea:visible, [contenteditable='true']:visible"
                    ).first.click(timeout=2000)
            except Exception:
                pass
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass
            await page.wait_for_timeout(600)
        return False
    finally:
        _FC_ALLOW["v"] = False
        _ATTACH_KEEP_PAGE["page"] = None
        _ATTACH_WORK_URL["v"] = ""
        if context is not None:
            try:
                keep = page
                await _close_all_extra_tabs(context, keep)
                page = await _ensure_attach_page(context, keep, work_url)
                await page.bring_to_front()
                await _install_page_guards(page)
            except Exception:
                pass


async def _count_large_images(page: Any) -> int:
    try:
        return int(
            await page.evaluate(
                """() => {
                  let n = 0;
                  for (const img of document.querySelectorAll('img')) {
                    const src = (img.currentSrc || img.src || '').toLowerCase();
                    if (src.includes('www.genspark.ai/api/files')) { n++; continue; }
                    const w = img.naturalWidth || img.width || 0;
                    const h = img.naturalHeight || img.height || 0;
                    if (w >= 256 && h >= 256) n++;
                  }
                  return n;
                }"""
            )
        )
    except Exception:
        return 0


async def _result_ready(page: Any) -> bool:
    """생성 결과 UI(api/files img · 다운로드 버튼 · 큰 미리보기)가 보이는지."""
    try:
        return bool(
            await page.evaluate(
                """() => {
                  // Genspark 결과: <img src="https://www.genspark.ai/api/files/s/...">
                  for (const img of document.querySelectorAll('img')) {
                    const src = (img.currentSrc || img.src || '').toLowerCase();
                    if (src.includes('www.genspark.ai/api/files')) return true;
                  }
                  // 다운로드 버튼/아이콘
                  const dl = document.querySelector(
                    'a[download], button[aria-label*="download" i], button[aria-label*="다운로드" i],'
                    + ' a[aria-label*="download" i], [data-testid*="download" i]'
                  );
                  if (dl) return true;
                  // SVG 화살표 다운로드 아이콘 근처 큰 이미지
                  const imgs = Array.from(document.querySelectorAll('img')).filter(img => {
                    const w = img.naturalWidth || img.width || 0;
                    const h = img.naturalHeight || img.height || 0;
                    return w >= 400 && h >= 300;
                  });
                  if (imgs.length === 0) return false;
                  // "이미지 생성" 결과 카드가 있고 큰 이미지가 있으면 완료로 간주
                  const t = (document.body && document.body.innerText) || '';
                  if (/이미지\\s*생성/.test(t) && imgs.length >= 1) return true;
                  // 우측 프리뷰 패널에 큰 이미지만 있어도 완료
                  return imgs.some(img => (img.naturalWidth || 0) >= 512);
                }"""
            )
        )
    except Exception:
        return False


async def _chat_has_pending_image_generation(page: Any) -> bool:
    """composer 바로 위 **최근** 채팅만 — 「이미지 생성」 카드에 결과 없을 때."""
    try:
        return bool(
            await page.evaluate(
                """() => {
                  const vh = window.innerHeight || 800;
                  const composerTop = vh - 140;
                  const stripTop = Math.max(0, composerTop - 620);
                  const inStrip = (r) => (
                    r.bottom >= stripTop && r.top <= composerTop - 8
                  );
                  const isResultImg = (img) => {
                    const src = (img.currentSrc || img.src || '').toLowerCase();
                    if (!src.includes('genspark.ai/api/files')) return false;
                    const w = img.naturalWidth || img.width || 0;
                    const h = img.naturalHeight || img.height || 0;
                    return w >= 160 && h >= 120;
                  };
                  const bubbleOf = (el) => {
                    let block = el;
                    for (let i = 0; i < 8 && block.parentElement; i++) {
                      const p = block.parentElement;
                      const pr = p.getBoundingClientRect();
                      if (pr.height > 900) break;
                      if (pr.width > (window.innerWidth || 900) * 0.92) break;
                      block = p;
                    }
                    return block;
                  };
                  const bubbleHasResult = (bubble) => {
                    for (const img of bubble.querySelectorAll('img')) {
                      if (isResultImg(img)) return true;
                    }
                    return false;
                  };
                  const blocks = [];
                  for (const el of document.querySelectorAll(
                    'div,section,article,li'
                  )) {
                    const r = el.getBoundingClientRect();
                    if (!inStrip(r)) continue;
                    if (r.height < 28 || r.width < 80) continue;
                    blocks.push({ el, top: r.top });
                  }
                  blocks.sort((a, b) => b.top - a.top);
                  let checked = 0;
                  for (const { el } of blocks) {
                    if (checked >= 4) break;
                    const raw = (el.innerText || '').replace(/\\s+/g, ' ').trim();
                    if (!raw) continue;
                    checked++;
                    const bubble = bubbleOf(el);
                    if (bubbleHasResult(bubble)) continue;
                    if (/이미지\\s*생성/.test(raw) && raw.length <= 120)
                      return true;
                    if (/생성\\s*중|generating/i.test(raw) && raw.length <= 80)
                      return true;
                    for (const spin of bubble.querySelectorAll(
                      '[class*="loading" i],[class*="spinner" i],'
                      + '[class*="pending" i],[aria-busy="true"]'
                    )) {
                      const sr = spin.getBoundingClientRect();
                      if (sr.width < 3 || sr.height < 3) continue;
                      if (!inStrip(sr)) continue;
                      return true;
                    }
                  }
                  return false;
                }"""
            )
        )
    except Exception:
        return False


async def _is_generating(page: Any) -> bool:
    """실제 **현재** 생성 진행 중인지 (과거 메시지 문구는 무시)."""
    if await _chat_has_pending_image_generation(page):
        return True
    try:
        return bool(
            await page.evaluate(
                """() => {
                  // 하단·최신 구간만 — 스크롤 위쪽 옛 Thinking/생성중 문구 오탐 방지
                  const full = ((document.body && document.body.innerText) || '');
                  const t = full.slice(Math.max(0, full.length - 3500));
                  // 완료 응답의 「백그라운드에서 진행」은 오탐 — 명령이 입력창에 남은 채 멈춤
                  const doneTalk = /성공적으로\\s*생성되었습니다/.test(t)
                    && /백그라운드에서\\s*진행|다음\\s*SRT|알려\\s*주세요|준비되어/.test(t);
                  if (!doneTalk) {
                    if (/백그라운드[^\\n]{0,24}처리\\s*중|아직\\s*처리\\s*중|
                      백그라운드에서\\s*진행|background[^.]{0,40}progress|
                      완료되면\\s*자동으로\\s*결과가\\s*전달|still\\s*being\\s*processed/i.test(t))
                      return true;
                    if (/(?:^|[\\n\\r])\\s*(?:Thinking|생각\\s*중|generating\\.?\\.|generation in progress|생성\\s*중|생성중)\\b/im.test(t))
                      return true;
                    if (/이미지를\\s*생성하고\\s*있|생성하고\\s*있습니다/.test(t))
                      return true;
                  }
                  const prog = document.querySelector(
                    '[role="progressbar"], [aria-busy="true"]'
                  );
                  if (prog) {
                    const r = prog.getBoundingClientRect();
                    if (r.width > 4 && r.height > 4) return true;
                  }
                  const vh = window.innerHeight || 800;
                  for (const b of document.querySelectorAll('button')) {
                    const al = (
                      (b.getAttribute('aria-label') || '') + ' '
                      + (b.getAttribute('title') || '') + ' '
                      + (b.innerText || '')
                    );
                    if (!/stop|중단|중지|cancel/i.test(al)) continue;
                    if (/요청이\\s*중단|stopped/i.test(al)) continue;
                    const r = b.getBoundingClientRect();
                    if (r.width > 8 && r.height > 8 && r.bottom > vh * 0.55 && r.top < vh - 2)
                      return true;
                  }
                  for (const el of document.querySelectorAll(
                    'textarea,[contenteditable="true"],[role="textbox"]'
                  )) {
                    const r = el.getBoundingClientRect();
                    if (r.width < 40 || r.bottom < vh * 0.42) continue;
                    if (el.disabled) return true;
                    if ((el.getAttribute('aria-disabled') || '') === 'true')
                      return true;
                    const ro = el.closest('[aria-busy="true"],[class*="loading" i]');
                    if (ro) return true;
                  }
                  return false;
                }"""
            )
        )
    except Exception:
        return False


async def _wait_editor_ready(page: Any, *, timeout_sec: float = 45.0) -> bool:
    """이어쓰기 입력란이 나타날 때까지 대기 (백그라운드 생성 해제 후)."""
    deadline = time.time() + max(3.0, float(timeout_sec))
    while time.time() < deadline:
        try:
            if await _is_generating(page):
                await page.wait_for_timeout(500)
                continue
            cands = await _prompt_editor_candidates(page, prefer_followup=True)
            if cands:
                return True
            cands = await _prompt_editor_candidates(page, prefer_followup=False)
            if cands:
                return True
        except Exception:
            pass
        try:
            await page.wait_for_timeout(400)
        except Exception:
            await asyncio.sleep(0.4)
    return False


async def _wait_idle_after_download(
    page: Any, *, timeout_sec: float = 40.0, stable_hits: int = 2
) -> bool:
    """이미지 저장 후: 생성이 끝나고 이어쓰기 칸이 안정될 때까지 대기.

    다운로드 직후 다음 SRT 명령을 넣으면, 이전 생성이 남은 채로
    Enter가 삼켜지거나 생성 없이 타임아웃 나는 경우가 있다.
    """
    deadline = time.time() + max(4.0, float(timeout_sec))
    idle_hits = 0
    while time.time() < deadline:
        try:
            if await _is_generating(page) or await _page_background_processing(
                page
            ):
                idle_hits = 0
                await page.wait_for_timeout(500)
                continue
            cands = await _prompt_editor_candidates(page, prefer_followup=True)
            if not cands:
                cands = await _prompt_editor_candidates(page, prefer_followup=False)
            if not cands:
                idle_hits = 0
                await page.wait_for_timeout(400)
                continue
            idle_hits += 1
            if idle_hits >= max(1, int(stable_hits)):
                return True
        except Exception:
            idle_hits = 0
        try:
            await page.wait_for_timeout(400)
        except Exception:
            await asyncio.sleep(0.4)
    return False


async def _largest_image_src(page: Any) -> str:
    try:
        return str(
            await page.evaluate(
                """() => {
                  // www.genspark.ai/api/files 우선 (마지막 결과)
                  let fileUrl = '';
                  for (const img of document.querySelectorAll('img')) {
                    const src = img.currentSrc || img.src || '';
                    if (src.toLowerCase().includes('www.genspark.ai/api/files'))
                      fileUrl = src;
                  }
                  if (fileUrl) return fileUrl;
                  let best = '', area = 0;
                  for (const img of document.querySelectorAll('img')) {
                    const w = img.naturalWidth || img.width || 0;
                    const h = img.naturalHeight || img.height || 0;
                    if (w * h > area && w >= 256) {
                      area = w * h;
                      best = img.currentSrc || img.src || '';
                    }
                  }
                  return best;
                }"""
            )
            or ""
        )
    except Exception:
        return ""


_REGEN_PREFIX = ""  # 재시도·재생성 메시지 사용 안 함



async def _failure_count(page: Any) -> int:
    """본문 ``Failure`` 개수. 우리 명령에 적어 둔 예시는 세지 않는다."""
    try:
        return int(
            await page.evaluate(
                """() => {
                  const raw = ((document.body && document.body.innerText) || '');
                  const t = raw.replace(
                    /실패면\\s*Failure[^\\n]{0,50}|Failure\\s*한\\s*줄만/gi, ' '
                  );
                  const m = t.match(/\\bFailure\\b/gi);
                  return m ? m.length : 0;
                }"""
            )
            or 0
        )
    except Exception:
        return 0


async def _page_shows_failure(page: Any, *, baseline_failures: int = 0) -> bool:
    """최신 응답에 Failure 가 새로 생겼는지.

    과거 Failure 는 무시한다 (한 번 Failure 나면 이후 대기가 즉시 끊기던 문제 방지).
    """
    try:
        now = await _failure_count(page)
        if now > max(0, int(baseline_failures)):
            return True
        # 최신(상단) 메시지 구간만
        return bool(
            await page.evaluate(
                """() => {
                  const t = ((document.body && document.body.innerText) || '')
                    .slice(0, 2200);
                  const failIdx = t.search(/\\bFailure\\b/i);
                  if (failIdx < 0) return false;
                  // 성공 문구가 Failure 보다 위에 있으면 과거 Failure
                  const okIdx = t.search(
                    /성공적으로\\s*생성|생성했습니다|api\\/files\\/s\\//i
                  );
                  if (okIdx >= 0 && okIdx < failIdx) return false;
                  return true;
                }"""
            )
        )
    except Exception:
        return False


async def _srt_success_without_image(page: Any, srt_sec: int) -> bool:
    """성공 문구만 있고 해당 SRT ``api/files`` 결과 그림이 없음."""
    if not await _srt_success_message_seen(page, srt_sec):
        return False
    try:
        return bool(
            await page.evaluate(
                """(sec) => {
                  const n = Number(sec) || 0;
                  const pad = String(n).padStart(3, '0');
                  const label = new RegExp(
                    'SRT[_\\\\s-]?(?:' + pad + '|' + n + ')\\\\b', 'i'
                  );
                  const okRe = /이미지가\\s*성공적으로\\s*생성|
                    성공적으로\\s*생성(?:되었습니다|됐습니다|됨)?/;
                  const isResultImg = (img) => {
                    const src = (img.currentSrc || img.src || '').toLowerCase();
                    if (!src.includes('genspark.ai/api/files')) return false;
                    const w = img.naturalWidth || img.width || 0;
                    const h = img.naturalHeight || img.height || 0;
                    return w >= 160 && h >= 120;
                  };
                  const bubbleOf = (el) => {
                    let block = el;
                    for (let i = 0; i < 10 && block.parentElement; i++) {
                      const p = block.parentElement;
                      const pr = p.getBoundingClientRect();
                      if (pr.height > 1200) break;
                      if (pr.width > (window.innerWidth || 900) * 0.96) break;
                      block = p;
                    }
                    return block;
                  };
                  const hasNearImg = (bubble) => {
                    for (const img of bubble.querySelectorAll('img')) {
                      if (isResultImg(img)) return true;
                    }
                    let sib = bubble;
                    for (let j = 0; j < 4; j++) {
                      sib = sib.nextElementSibling;
                      if (!sib) break;
                      for (const img of sib.querySelectorAll('img')) {
                        if (isResultImg(img)) return true;
                      }
                    }
                    return false;
                  };
                  for (const el of document.querySelectorAll(
                    'div,section,article,li,p,span'
                  )) {
                    const t = (el.innerText || '').replace(/\\s+/g, ' ').trim();
                    if (t.length > 220 || !label.test(t)) continue;
                    if (!okRe.test(t)) continue;
                    if (el.closest(
                      'textarea,input,[contenteditable="true"]'
                    )) continue;
                    if (!hasNearImg(bubbleOf(el))) return true;
                  }
                  return false;
                }""",
                int(srt_sec),
            )
        )
    except Exception:
        return False


async def _srt_success_message_seen(page: Any, srt_sec: int) -> bool:
    """``SRT_XXX 이미지가 성공적으로 생성되었습니다`` 성공 문구 감지.

    요청 문구(생성해줘)는 제외. '성공적으로 생성'을 우선한다.
    """
    try:
        return bool(
            await page.evaluate(
                """(sec) => {
                  const n = Number(sec) || 0;
                  const pad = String(n).padStart(3, '0');
                  const label = new RegExp(
                    'SRT[_\\\\s-]?(?:' + pad + '|' + n + ')\\\\b', 'i'
                  );
                  // 사용자 확인 문구: "SRT_xxx 이미지가 성공적으로 생성되었습니다"
                  const okStrict = /이미지가\\s*성공적으로\\s*생성/;
                  const okLoose = /성공적으로\\s*생성(?:되었습니다|됐습니다|됨)?/;
                  const okAlt = /이미지를\\s*생성했습니다/;
                  const tw = document.createTreeWalker(
                    document.body, NodeFilter.SHOW_TEXT
                  );
                  let node;
                  while ((node = tw.nextNode())) {
                    const t = (node.textContent || '').replace(/\\s+/g, ' ').trim();
                    if (!label.test(t)) continue;
                    if (t.length > 180) continue;
                    if (/쓰지\\s*말|화면에\\s*나온\\s*뒤에만|지금\\s*실제로\\s*생성|이전\\s*SRT\\s*무시|말풍선/.test(t))
                      continue;
                    if (/생성해\\s*줘|생성해줘/i.test(t)
                        && !okStrict.test(t) && !okLoose.test(t) && !okAlt.test(t))
                      continue;
                    if (!(okStrict.test(t) || okLoose.test(t) || okAlt.test(t)))
                      continue;
                    const el = node.parentElement;
                    if (!el) continue;
                    if (el.closest(
                      'textarea, input, [contenteditable="true"]'
                    )) continue;
                    return true;
                  }
                  for (const el of document.querySelectorAll(
                    'div, section, article, li, p'
                  )) {
                    const t = ((el.innerText || '') + '').replace(/\\s+/g, ' ').trim();
                    if (t.length > 180) continue;
                    if (!label.test(t)) continue;
                    if (/쓰지\\s*말|화면에\\s*나온\\s*뒤에만|지금\\s*실제로\\s*생성|이전\\s*SRT\\s*무시|말풍선/.test(t))
                      continue;
                    if (!(okStrict.test(t) || okLoose.test(t))) continue;
                    if (el.closest(
                      'textarea, input, [contenteditable="true"]'
                    )) continue;
                    return true;
                  }
                  return false;
                }""",
                int(srt_sec),
            )
        )
    except Exception:
        return False


async def _generation_error_ui(page: Any) -> bool:
    """응답 옆 회색 느낌표·error 배지 (이미지는 없고 성공 문구만 올 때)."""
    try:
        return bool(
            await page.evaluate(
                """() => {
                  const vh = window.innerHeight || 800;
                  const vw = window.innerWidth || 1200;
                  const nodes = document.querySelectorAll(
                    'button, [role="button"], [aria-label], [title], svg, img, span'
                  );
                  for (const el of nodes) {
                    const r = el.getBoundingClientRect();
                    if (r.width < 10 || r.width > 44 || r.height < 10 || r.height > 44)
                      continue;
                    if (r.left < vw * 0.50 || r.top < 48 || r.bottom > vh - 70)
                      continue;
                    const t = (
                      (el.getAttribute('aria-label') || '') + ' '
                      + (el.getAttribute('title') || '') + ' '
                      + (el.getAttribute('data-tooltip') || '') + ' '
                      + (el.className || '')
                    ).toLowerCase();
                    if (/error|fail|실패|warning|경고|exclaim/.test(t))
                      return true;
                  }
                  return false;
                }"""
            )
        )
    except Exception:
        return False


async def _collect_genspark_file_urls(page: Any) -> list[str]:
    """페이지의 genspark api/files img URL 목록."""
    try:
        raw = await page.evaluate(
            """() => {
              const out = [];
              for (const img of document.querySelectorAll('img')) {
                const src = img.currentSrc || img.src
                  || img.getAttribute('data-src') || '';
                if (/genspark\\.ai\\/api\\/files/i.test(src)) out.push(src);
              }
              return out;
            }"""
        )
        if isinstance(raw, list):
            return [str(u) for u in raw if u]
    except Exception:
        pass
    return []


async def _first_unseen_file_url(
    page: Any,
    *,
    forbid_keys: set[str] | None = None,
    last_saved: str = "",
    baseline: str = "",
) -> str:
    for src in await _collect_genspark_file_urls(page):
        if _is_unseen_file_url(
            src,
            forbid_keys=forbid_keys,
            last_saved=last_saved,
            baseline=baseline,
        ):
            return src
    return ""


async def _assistant_skipped_generation(page: Any, srt_sec: int) -> bool:
    """새 그림 없이 이전 SRT 이야지만 하는지 (예: SRT_165 이전 턴)."""
    try:
        return bool(
            await page.evaluate(
                """(sec) => {
                  const t = ((document.body && document.body.innerText) || '')
                    .slice(0, 4500);
                  if (/백그라운드에서\\s*진행|생성하고\\s*있|generating/i.test(t))
                    return false;
                  if (/준비\\s*완료|이미지\\s*생성/.test(t))
                    return false;
                  const skip = /이전\\s*턴|이미\\s*생성되어\\s*완료|Generated image metadata|어떤 SRT 이미지를 생성/;
                  if (!skip.test(t)) return false;
                  const n = Number(sec) || 0;
                  const pad = String(n).padStart(3, '0');
                  const selfOk = new RegExp(
                    'SRT[_\\\\s-]?(?:' + pad + '|' + n
                    + ')\\\\s*이미지가\\\\s*성공적으로\\\\s*생성'
                  );
                  if (selfOk.test(t) && /api\\/files/i.test(t)) return false;
                  return true;
                }""",
                int(srt_sec),
            )
        )
    except Exception:
        return False


async def _wait_generation_done(
    page: Any,
    *,
    baseline_count: int,
    prev_src: str = "",
    timeout_sec: int = 120,
    context: Any | None = None,
    baseline_failures: int = 0,
    srt_sec: int | None = None,
    baseline_near_src: str = "",
    forbid_keys: set[str] | None = None,
    last_saved_src: str = "",
) -> tuple[bool, Any]:
    """성공 메시지 + 그 아래 **아직 받지 않은** ``api/files`` 이미지까지 대기.

    직전 저장 URL·seen URL 은 새 이미지로 보지 않는다.
    ``(완료여부, page)`` 반환.
    """
    del baseline_count  # 호환용
    deadline = asyncio.get_event_loop().time() + max(120, int(timeout_sec))
    started = asyncio.get_event_loop().time()
    t0 = time.perf_counter()
    stable = 0
    forbid = set(forbid_keys or set())
    last_saved = last_saved_src or prev_src
    fail_base = max(0, int(baseline_failures))
    last_progress_log = 0.0
    _tab_log(
        f"⏱ 생성대기 시작 SRT_{(srt_sec or 0):03d} max={max(120, int(timeout_sec))}s"
    )

    async def _recover() -> Any:
        nonlocal page
        if context is None:
            try:
                if page is not None and not page.is_closed():
                    return page
            except Exception:
                pass
            raise BrowserClosedError(BROWSER_CLOSED_MSG)
        if not _context_has_live_page(context):
            raise BrowserClosedError(BROWSER_CLOSED_MSG)
        try:
            if page is not None and not page.is_closed():
                return page
        except Exception:
            pass
        _tab_log("생성대기: 작업탭 닫힘 → 복구")
        page = await _alive_page(context, page)
        _tab_log(f"생성대기: 복구됨 · {await _tab_snapshot(context, page)}")
        return page

    try:
        await page.wait_for_timeout(600)
    except Exception as ex:
        if is_browser_closed_error(ex):
            raise BrowserClosedError(BROWSER_CLOSED_MSG) from ex
        if "closed" in str(ex).lower() and context is not None:
            page = await _recover()
        else:
            raise
    while asyncio.get_event_loop().time() < deadline:
        try:
            page = await _recover()
            if await _page_shows_failure(page, baseline_failures=fail_base):
                _tab_log(
                    f"⏱ 생성대기 Failure {_timing_sec(t0)}s "
                    f"SRT_{(srt_sec or 0):03d}"
                )
                return False, page
            if srt_sec is not None and await _srt_label_shows_failure(
                page, int(srt_sec)
            ):
                if not await _srt_success_message_seen(page, int(srt_sec)):
                    _tab_log(
                        f"⏱ 생성대기 SRT Failure {_timing_sec(t0)}s "
                        f"SRT_{int(srt_sec):03d}"
                    )
                    return False, page

            busy = await _is_generating(page)
            near = ""
            if srt_sec is not None:
                near = (
                    await _file_src_near_srt_label(
                        page, int(srt_sec), forbid_keys=forbid
                    )
                    or ""
                ).strip()
            else:
                near = await _first_unseen_file_url(
                    page,
                    forbid_keys=forbid,
                    last_saved=last_saved,
                    baseline=baseline_near_src,
                )
            url_ok = _is_unseen_file_url(
                near,
                forbid_keys=forbid,
                last_saved=last_saved,
                baseline=baseline_near_src,
            )

            elapsed = _timing_sec(t0)
            if elapsed - last_progress_log >= 15.0:
                last_progress_log = elapsed
                _tab_log(
                    f"⏱ 생성대기 진행 {_timing_sec(t0)}s "
                    f"SRT_{(srt_sec or 0):03d} busy={busy} url_ok={url_ok} "
                    f"src={_short_src(near)}"
                )
                # 한도 배너/토스트 주기 점검
                hit = await detect_limit_on_page(page)
                if hit is not None:
                    _tab_log(
                        f"⏱ 생성대기 한도감지 {_timing_sec(t0)}s "
                        f"SRT_{(srt_sec or 0):03d} · {hit.message} · {hit.snippet[:120]}"
                    )
                    raise_limit_error(hit)

            pending_card = await _chat_has_pending_image_generation(page)
            success_seen = (
                srt_sec is not None
                and await _srt_success_message_seen(page, int(srt_sec))
            )
            if success_seen and url_ok:
                pending_card = False
            if url_ok and not busy and not pending_card:
                stable += 1
                if stable >= 2:
                    await page.wait_for_timeout(250)
                    _tab_log(
                        f"⏱ 생성대기 완료 {_timing_sec(t0)}s "
                        f"SRT_{(srt_sec or 0):03d} "
                        f"{_short_src(near)}"
                    )
                    return True, page
            elif url_ok and not pending_card:
                stable += 1
                if stable >= 3:
                    _tab_log(
                        f"⏱ 생성대기 완료(busy무시) {_timing_sec(t0)}s "
                        f"SRT_{(srt_sec or 0):03d} "
                        f"{_short_src(near)}"
                    )
                    return True, page
            else:
                if (
                    srt_sec is not None
                    and not busy
                    and not pending_card
                    and elapsed >= 10.0
                    and await _srt_success_without_image(page, int(srt_sec))
                ):
                    _tab_log(
                        f"⏱ 생성대기 성공문구만(이미지없음) "
                        f"{_timing_sec(t0)}s SRT_{int(srt_sec):03d}"
                    )
                    return False, page
                if (
                    srt_sec is not None
                    and not busy
                    and await _srt_success_message_seen(page, int(srt_sec))
                    and await _generation_error_ui(page)
                ):
                    _tab_log(
                        f"⏱ 생성대기 성공문구·오류아이콘 "
                        f"{_timing_sec(t0)}s SRT_{int(srt_sec):03d}"
                    )
                    return False, page
                if (
                    srt_sec is not None
                    and (asyncio.get_event_loop().time() - started) >= 18
                    and await _assistant_skipped_generation(page, int(srt_sec))
                ):
                    _tab_log(
                        f"⏱ 생성대기 스킵감지 {_timing_sec(t0)}s "
                        f"SRT_{int(srt_sec):03d}"
                    )
                    return False, page
                stable = 0
            # 생성 중은 0.7s, 이미 보이면 0.4s
            await page.wait_for_timeout(700 if busy and not url_ok else 400)
        except BrowserClosedError:
            _tab_log("생성대기: 브라우저 종료 — 즉시 중단")
            raise
        except Exception as ex:
            if is_browser_closed_error(ex):
                raise BrowserClosedError(BROWSER_CLOSED_MSG) from ex
            if "closed" in str(ex).lower() and context is not None:
                _tab_log(f"생성대기 예외(closed): {ex}")
                try:
                    page = await _recover()
                except BrowserClosedError:
                    _tab_log("생성대기: 브라우저 종료 — 즉시 중단")
                    raise
                await asyncio.sleep(0.4)
                continue
            raise

    # 생성 지연: 제한 직후 늦게 붙는 이미지는 해당 SRT 성공 문구 아래만 인정
    if srt_sec is not None:
        extra_until = asyncio.get_event_loop().time() + 25.0
        while asyncio.get_event_loop().time() < extra_until:
            if context is not None and not _context_has_live_page(context):
                raise BrowserClosedError(BROWSER_CLOSED_MSG)
            near = (
                await _file_src_near_srt_label(
                    page, int(srt_sec), forbid_keys=forbid
                )
                or ""
            ).strip()
            if _is_unseen_file_url(
                near,
                forbid_keys=forbid,
                last_saved=last_saved,
                baseline=baseline_near_src,
            ):
                _tab_log(
                    f"⏱ 생성대기 지연도착 {_timing_sec(t0)}s "
                    f"SRT_{int(srt_sec):03d} "
                    f"{_short_src(near)}"
                )
                return True, page
            try:
                await page.wait_for_timeout(800)
            except Exception as ex:
                if is_browser_closed_error(ex):
                    raise BrowserClosedError(BROWSER_CLOSED_MSG) from ex
                raise
    else:
        near = await _first_unseen_file_url(
            page,
            forbid_keys=forbid,
            last_saved=last_saved,
            baseline=baseline_near_src,
        )
        if near:
            _tab_log(
                f"⏱ 생성대기 시간초과·이미지로 완료 {_timing_sec(t0)}s "
                f"SRT_{(srt_sec or 0):03d}"
            )
            return True, page
    if await _page_shows_failure(page, baseline_failures=fail_base):
        return False, page
    # 타임아웃 직전 — 한도 배너 우선
    hit = await detect_limit_on_page(page)
    if hit is not None:
        _tab_log(
            f"⏱ 생성대기 한도감지(시간초과) {_timing_sec(t0)}s "
            f"SRT_{(srt_sec or 0):03d} · {hit.message} · {hit.snippet[:120]}"
        )
        raise_limit_error(hit)
    _tab_log(
        f"⏱ 생성대기 시간초과 {_timing_sec(t0)}s "
        f"SRT_{(srt_sec or 0):03d} "
        f"(새 api/files 이미지 없음, max={max(120, int(timeout_sec))}s)"
    )
    return False, page


async def _genspark_file_src(page: Any) -> str:
    """마지막 ``www.genspark.ai/api/files…`` img src만."""
    try:
        return str(
            await page.evaluate(
                """() => {
                  let last = '';
                  for (const img of document.querySelectorAll('img')) {
                    const src = img.currentSrc || img.src
                      || img.getAttribute('data-src') || '';
                    if (src.toLowerCase().includes('www.genspark.ai/api/files')
                        || src.toLowerCase().includes('genspark.ai/api/files'))
                      last = src;
                  }
                  return last;
                }"""
            )
            or ""
        )
    except Exception:
        return ""


async def _file_src_near_srt_label(
    page: Any,
    srt_sec: int,
    *,
    forbid_keys: set[str] | None = None,
) -> str:
    """해당 SRT 요청(또는 성공 문구) 근처의 미수신 이미지 URL.

    성공 문구가 없어도, 방금 보낸 ``SRT_XXX`` 명령 **아래**
    (「이미지 생성」 카드) 그림을 고른다. 다음 SRT 요청보다 아래는 쓰지 않는다.
    """
    forbid = [k for k in (forbid_keys or set()) if k]
    try:
        return str(
            await page.evaluate(
                """({sec, forbid}) => {
                  const n = Number(sec) || 0;
                  const pad = String(n).padStart(3, '0');
                  const re = new RegExp(
                    'SRT[_\\\\s-]?(?:' + pad + '|' + n + ')\\\\b', 'i'
                  );
                  const otherSrt = /SRT[_\\\\s-]?\\\\d+/i;
                  const instr = /쓰지\\s*말|화면에\\s*나온\\s*뒤에만|지금\\s*실제로\\s*생성|이전\\s*SRT\\s*무시|말풍선/;
                  const keyOf = (src) => {
                    const u = String(src).split('#')[0].split('?')[0]
                      .toLowerCase().replace(/\\/$/, '');
                    const m = u.match(
                      /https?:\\/\\/(?:www\\.)?genspark\\.ai\\/api\\/files\\/(?:s\\/)?[^/\\s]+/
                    );
                    return m ? m[0] : u;
                  };
                  const banned = new Set(
                    (forbid || []).map(u => keyOf(u)).filter(Boolean)
                  );
                  const bubbleOf = (el) => {
                    let block = el;
                    for (let i = 0; i < 8 && block.parentElement; i++) {
                      const p = block.parentElement;
                      const big = p.querySelector
                        && p.querySelector('img');
                      if (big) {
                        const ir = big.getBoundingClientRect();
                        if (ir.width >= 200 && ir.height >= 160) break;
                      }
                      const pb = p.getBoundingClientRect();
                      if (pb.height > 1800) break;
                      if (pb.width > (window.innerWidth || 1200) * 0.9) break;
                      block = p;
                    }
                    return block.getBoundingClientRect();
                  };
                  const markers = [];
                  const otherTops = [];
                  const tw = document.createTreeWalker(
                    document.body, NodeFilter.SHOW_TEXT
                  );
                  let node;
                  while ((node = tw.nextNode())) {
                    const t = node.textContent || '';
                    const el = node.parentElement;
                    if (!el) continue;
                    if (el.closest(
                      'textarea, input, [contenteditable="true"], nav, header'
                    )) continue;
                    const raw = el.getBoundingClientRect();
                    if (raw.width < 2 && raw.height < 2) continue;
                    if (!re.test(t)) {
                      if (otherSrt.test(t)) otherTops.push(raw.top);
                      continue;
                    }
                    const box = bubbleOf(el);
                    const isFail = /\\\\bFailure\\\\b/i.test(t)
                      || (/Failure/i.test(
                        (el.closest('div,section,article,li') || el).innerText || ''
                      ) && raw.top < 400);
                    const isRes = /이미지가\\s*성공적으로\\s*생성/.test(t)
                      && !instr.test(t);
                    markers.push({
                      y: box.bottom,
                      top: box.top,
                      req: !isRes,
                      res: isRes,
                      fail: isFail,
                    });
                  }
                  if (!markers.length) return '';
                  const imgs = [];
                  const visit = (root) => {
                    if (!root || !root.querySelectorAll) return;
                    for (const img of root.querySelectorAll('img')) {
                      const src = img.currentSrc || img.src
                        || img.getAttribute('data-src') || '';
                      if (!src) continue;
                      const r = img.getBoundingClientRect();
                      if (r.width < 48 || r.height < 48) continue;
                      const isFile = /genspark\\.ai\\/api\\/files/i.test(src);
                      const okSrc = /^https?:/i.test(src) || /^blob:/i.test(src);
                      if (!isFile && !(okSrc && r.width >= 200 && r.height >= 160))
                        continue;
                      if (banned.has(keyOf(src))) continue;
                      imgs.push({
                        src, y: r.top, bottom: r.bottom, file: isFile,
                      });
                    }
                    for (const el of root.querySelectorAll('*')) {
                      if (el.shadowRoot) visit(el.shadowRoot);
                    }
                  };
                  visit(document);
                  if (!imgs.length) return '';
                  const stopAfter = (y) => {
                    let s = 1e12;
                    for (const t of otherTops) {
                      if (t > y + 24 && t < s) s = t;
                    }
                    return s === 1e12 ? 0 : s;
                  };
                  const pickNear = (m, mode) => {
                    const nextStop = mode === 'below' ? stopAfter(m.y) : 0;
                    const tryPick = (filesOnly) => {
                      let best = null;
                      let bestD = 1e12;
                      for (const im of imgs) {
                        if (filesOnly && !im.file) continue;
                        let d = 1e12;
                        if (mode === 'above') {
                          if (im.bottom > m.top + 24) continue;
                          d = m.top - im.bottom;
                          if (d > 220) continue;
                        } else {
                          if (im.y + 8 < m.y) continue;
                          if (nextStop && im.y >= nextStop - 4) continue;
                          d = im.y - m.y;
                          if (d > 6000) continue;
                        }
                        if (d < bestD) { bestD = d; best = im; }
                      }
                      return best;
                    };
                    return tryPick(true) || tryPick(false);
                  };
                  const resMs = markers.filter(m => m.res && !(m.fail && !m.res));
                  for (const m of resMs) {
                    const above = pickNear(m, 'above');
                    if (above) return above.src;
                    const below = pickNear(m, 'below');
                    if (below) return below.src;
                  }
                  const reqMs = markers.filter(m => m.req)
                    .sort((a, b) => b.y - a.y);
                  for (const m of reqMs) {
                    const below = pickNear(m, 'below');
                    if (below) return below.src;
                  }
                  return '';
                }""",
                {"sec": int(srt_sec), "forbid": forbid},
            )
            or ""
        )
    except Exception as ex:
        _tab_log(f"SRT 근접 이미지 탐색 실패: {ex}")
        return ""


async def _srt_label_shows_failure(page: Any, srt_sec: int) -> bool:
    """해당 SRT_XXX 근처(본문)에 Failure 가 있는지."""
    try:
        return bool(
            await page.evaluate(
                """(sec) => {
                  const n = Number(sec) || 0;
                  const pad = String(n).padStart(3, '0');
                  const re = new RegExp(
                    'SRT[_\\\\s-]?(?:' + pad + '|' + n + ')\\\\b', 'i'
                  );
                  const blocks = document.querySelectorAll(
                    'div, section, article, li, p'
                  );
                  for (const el of blocks) {
                    const t = (el.innerText || '').slice(0, 1200);
                    if (!re.test(t)) continue;
                    if (/쓰지\\s*말|화면에\\s*나온\\s*뒤에만|지금\\s*실제로\\s*생성|말풍선/.test(t))
                      continue;
                    if (/\\\\bFailure\\\\b/i.test(t)) return true;
                  }
                  return false;
                }""",
                int(srt_sec),
            )
        )
    except Exception:
        return False


def _image_url_key(src: str) -> str:
    """중복 판별 키. api/files 우선, 없으면 blob·일반 이미지 URL."""
    key = normalize_genspark_file_url(src)
    if key:
        return key
    u = (src or "").strip()
    if not u or is_tracking_url(u):
        return ""
    if u.startswith("blob:"):
        return u
    if looks_like_image_url(u) or u.startswith(("http://", "https://")):
        return u.split("#", 1)[0].split("?", 1)[0].rstrip("/").lower()
    return ""


def _short_src(src: str) -> str:
    return (_image_url_key(src) or src or "")[:90]


def _is_unseen_file_url(
    src: str,
    *,
    forbid_keys: set[str] | None = None,
    last_saved: str = "",
    baseline: str = "",
) -> bool:
    """이미 받은·직전 저장·제출 전 근접 URL 이 아닌 새 이미지인지."""
    key = _image_url_key(src)
    if not key:
        return False
    if forbid_keys and key in forbid_keys:
        return False
    last_k = _image_url_key(last_saved)
    if last_k and key == last_k:
        return False
    base_k = _image_url_key(baseline)
    if base_k and key == base_k:
        return False
    return True


def _file_url_map_path(png_dir: Path) -> Path:
    return Path(png_dir) / ".genspark_file_urls.json"


def _load_seen_file_urls(png_dir: Path) -> dict[str, str]:
    """``{normalized_url: SRT_XXX.png}``."""
    path = _file_url_map_path(png_dir)
    if not path.is_file():
        return {}
    try:
        import json

        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items() if k and v}
    except Exception:
        pass
    return {}


def _save_seen_file_urls(png_dir: Path, mapping: dict[str, str]) -> None:
    path = _file_url_map_path(png_dir)
    try:
        import json

        path.write_text(
            json.dumps(mapping, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass


async def _salvage_late_images(
    page: Any,
    png_dir: Path,
    pending_secs: list[int],
    seen_file_urls: dict[str, str],
) -> str:
    """타임아웃 후 늦게 뜬 이미지를 원래 SRT 파일명으로 저장."""
    try:
        if page is None or page.is_closed():
            raise BrowserClosedError(BROWSER_CLOSED_MSG)
    except BrowserClosedError:
        raise
    except Exception:
        raise BrowserClosedError(BROWSER_CLOSED_MSG)
    last_src = ""
    keep: list[int] = []
    for sec in pending_secs:
        if png_already_exists(png_dir, sec):
            continue
        dest = png_dir / srt_png_name(sec)
        try:
            saved, file_src = await _save_latest_image_to(
                page,
                dest,
                forbid_keys=set(seen_file_urls.keys()),
                srt_sec=sec,
            )
        except BrowserClosedError:
            raise
        except Exception as ex:
            if is_browser_closed_error(ex):
                raise BrowserClosedError(BROWSER_CLOSED_MSG) from ex
            _tab_log(f"늦은이미지 회수 실패 SRT_{sec:03d}: {ex}")
            keep.append(sec)
            continue
        key = _image_url_key(file_src)
        if key:
            seen_file_urls[key] = dest.name
            _save_seen_file_urls(png_dir, seen_file_urls)
        last_src = file_src
        _LAST_SCENE_REF["sec"] = int(sec)
        _LAST_SCENE_REF["path"] = str(saved.resolve())
        _LAST_SCENE_REF["file_url"] = file_src or ""
        _tab_log(f"늦은이미지 회수 SRT_{sec:03d} → {saved.name}")
    pending_secs[:] = keep[-8:]
    return last_src


async def _save_latest_image_to(
    page: Any,
    dest: Path,
    *,
    prefer_button: bool = False,
    forbid_keys: set[str] | None = None,
    require_new_vs: str = "",
    srt_sec: int | None = None,
) -> tuple[Path, str]:
    """``api/files`` 이미지 다운로드 → dest(SRT_XXX.png).

    ``srt_sec`` 가 있으면 본문 ``SRT_XXX`` 성공 메시지 아래 **미수신** 이미지를 우선한다.
    이미 받은 URL 이면 저장하지 않고 오류.
    반환: ``(경로, 원본 URL)``.
    """
    del prefer_button
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    from scene_image.download import download_url

    file_src = ""
    if srt_sec is not None:
        file_src = (
            await _file_src_near_srt_label(
                page, int(srt_sec), forbid_keys=forbid_keys
            )
            or ""
        ).strip()
        if file_src:
            _tab_log(
                f"SRT_{int(srt_sec):03d} 근접 이미지 "
                f"{_short_src(file_src)}"
            )
        else:
            raise RuntimeError(
                f"SRT_{int(srt_sec):03d} 요청 아래 새 이미지가 없습니다."
            )
    if not file_src:
        file_src = (
            await _first_unseen_file_url(
                page,
                forbid_keys=forbid_keys,
                last_saved=require_new_vs,
                baseline=require_new_vs,
            )
            or ""
        ).strip()
        if file_src:
            _tab_log(
                f"폴백: 미수신 api/files "
                f"{_short_src(file_src)}"
            )
    if not file_src:
        raise RuntimeError(
            f"SRT_{int(srt_sec or 0):03d} 새 이미지가 없습니다."
        )
    if is_tracking_url(file_src) or not (
        is_genspark_file_url(file_src)
        or looks_like_image_url(file_src)
        or file_src.startswith(("http://", "https://", "blob:"))
    ):
        raise RuntimeError(
            "새 이미지 URL을 찾지 못했습니다. "
            "생성이 끝난 뒤 다시 시도하세요."
        )

    key = _image_url_key(file_src)
    if not _is_unseen_file_url(
        file_src,
        forbid_keys=forbid_keys,
        last_saved=require_new_vs,
        baseline=require_new_vs,
    ):
        raise RuntimeError(
            f"이미 받은 api/files 이미지입니다 (중복 저장 방지).\n{key[:120]}"
        )
    _tab_log(f"다운로드 files URL={key[:120] or file_src[:120]} → {dest.name}")
    t_dl = time.perf_counter()

    try:
        if file_src.startswith("blob:"):
            raise RuntimeError("blob URL")
        download_url(file_src, dest)
        if dest.is_file() and dest.stat().st_size >= 512:
            _tab_log(
                f"⏱ 다운로드 직접 {_timing_sec(t_dl)}s "
                f"size={dest.stat().st_size} {dest.name}"
            )
            return dest, file_src
    except Exception as ex:
        _tab_log(
            f"⏱ 다운로드 직접실패 {_timing_sec(t_dl)}s · 페이지 fetch 재시도: {ex}"
        )

    t_fetch = time.perf_counter()
    b64 = await page.evaluate(
        """async (u) => {
          const r = await fetch(u, { credentials: 'include' });
          if (!r.ok) return '';
          const buf = await r.arrayBuffer();
          const bytes = new Uint8Array(buf);
          let binary = '';
          const chunk = 0x8000;
          for (let i = 0; i < bytes.length; i += chunk) {
            binary += String.fromCharCode.apply(
              null, bytes.subarray(i, i + chunk)
            );
          }
          return btoa(binary);
        }""",
        file_src,
    )
    if not b64:
        raise RuntimeError(f"api/files 다운로드 실패: {file_src[:120]}")
    dest.write_bytes(base64.b64decode(b64))
    if not dest.is_file() or dest.stat().st_size < 512:
        raise RuntimeError(f"다운로드 검증 실패: {dest.name}")
    _tab_log(
        f"⏱ 다운로드 fetch {_timing_sec(t_fetch)}s "
        f"size={dest.stat().st_size} {dest.name}"
    )
    return dest, file_src


async def _save_storage_state(context: Any, path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        await context.storage_state(path=str(path.resolve()))
    except Exception:
        pass


_SAME_TAB_SCRIPT = """
(() => {
  if (window.__wisdomSameTabV3) return;
  window.__wisdomSameTabV3 = true;
  const go = (url) => {
    try {
      if (url && typeof url === 'string' && url.length && url !== 'about:blank') {
        window.location.assign(url);
      }
    } catch (e) {}
  };
  try {
    window.open = function(url) { go(url); return window; };
  } catch (e) {}
  try {
    Object.defineProperty(window, 'open', {
      configurable: true,
      writable: true,
      value: function(url) { go(url); return window; }
    });
  } catch (e) {}
  const retarget = (el) => {
    try {
      if (!el || !el.closest) return null;
      const a = el.closest('a');
      if (a && a.getAttribute('target') === '_blank') a.setAttribute('target', '_self');
      const f = el.closest('form');
      if (f && f.getAttribute('target') === '_blank') f.setAttribute('target', '_self');
      return a;
    } catch (err) { return null; }
  };
  const blockNew = (e) => {
    try {
      const a = retarget(e.target);
      if (!a) return;
      if (e.ctrlKey || e.metaKey || e.shiftKey || e.button === 1
          || (a.getAttribute('target') || '').toLowerCase() === '_blank') {
        const href = a.href || a.getAttribute('href') || '';
        if (href && !href.startsWith('#') && !href.startsWith('javascript:')) {
          e.preventDefault();
          e.stopPropagation();
          go(href);
        }
      }
    } catch (err) {}
  };
  document.addEventListener('click', blockNew, true);
  document.addEventListener('auxclick', blockNew, true);
  document.addEventListener('mousedown', (e) => { retarget(e.target); }, true);
  try {
    const mo = new MutationObserver(() => {
      document.querySelectorAll('a[target="_blank"], form[target="_blank"]').forEach(el => {
        el.setAttribute('target', '_self');
      });
    });
    mo.observe(document.documentElement, { childList: true, subtree: true, attributes: true, attributeFilter: ['target'] });
  } catch (err) {}
})();
"""

_ANTI_MAGIC_REDRAW_SCRIPT = """
(() => {
  if (window.__wisdomNoMagicRedraw) return;
  window.__wisdomNoMagicRedraw = true;
  const badText = (s) => /magic\\s*redraw|매직\\s*다시\\s*그리|inpaint|edit\\s*image|이미지\\s*편집|brush\\s*tool/i.test(s || '');
  const labelOf = (el) => {
    if (!el) return '';
    return (
      (el.innerText || '') + ' '
      + (el.getAttribute('aria-label') || '') + ' '
      + (el.getAttribute('title') || '')
    ).slice(0, 400);
  };
  const inComposer = (el) => {
    try {
      return !!el.closest(
        '[class*="composer" i], [class*="prompt" i], footer, '
        + '[class*="input" i], [class*="chat-input" i], form'
      );
    } catch (e) { return false; }
  };
  const block = (e) => {
    try {
      const el = e.target;
      if (!el || !el.closest) return;
      if (badText(labelOf(el))) {
        e.preventDefault();
        e.stopPropagation();
        e.stopImmediatePropagation();
        return;
      }
      let node = el;
      for (let i = 0; i < 8 && node; i++, node = node.parentElement) {
        if (badText(labelOf(node))) {
          e.preventDefault();
          e.stopPropagation();
          e.stopImmediatePropagation();
          return;
        }
      }
      const img = el.tagName === 'IMG' ? el : (el.querySelector && el.querySelector('img'));
      if (img && !inComposer(el)) {
        const w = img.naturalWidth || img.width || 0;
        const h = img.naturalHeight || img.height || 0;
        if (w >= 260 && h >= 260) {
          e.preventDefault();
          e.stopPropagation();
          e.stopImmediatePropagation();
        }
      }
    } catch (err) {}
  };
  document.addEventListener('click', block, true);
  document.addEventListener('mousedown', block, true);
  document.addEventListener('auxclick', block, true);
})();
"""


def _ai_image_bare_url(url: str) -> str:
    """쿼리·해시 없는 ai_image 랜딩 (편집 UI 상태 제거)."""
    u = (url or GENSPARK_AI_IMAGE_URL).strip()
    bare = u.split("#")[0].split("?")[0].rstrip("/")
    if "ai_image" not in bare.lower() and "ai-image" not in bare.lower():
        return GENSPARK_AI_IMAGE_URL.rstrip("/")
    return bare


def _is_wrong_agent_url(url: str) -> bool:
    """AI Image 가 아닌 Genspark 에이전트(동영상 등)."""
    u = (url or "").lower()
    if not u or "genspark" not in u:
        return False
    if "video_generation" in u or "video_agent" in u:
        return True
    if "/agents" in u and "type=" in u and "image_generation" not in u:
        return True
    if "imageurl=" in u and "chat_now" in u:
        return True
    return False


async def _page_is_magic_redraw(page: Any) -> bool:
    """ai_image URL 이더라도 「매직 다시 그리기」 편집 UI 인지."""
    try:
        return bool(
            await page.evaluate(
                """() => {
                  const t = ((document.body && document.body.innerText) || '')
                    .slice(0, 5000);
                  if (/매직\\s*다시\\s*그리|magic\\s*redraw/i.test(t))
                    return true;
                  const hasBrush = /브러시|brush/i.test(t)
                    && /재설정|reset/i.test(t);
                  if (!hasBrush) return false;
                  for (const img of document.querySelectorAll('img')) {
                    const w = img.naturalWidth || img.width || 0;
                    const h = img.naturalHeight || img.height || 0;
                    if (w >= 320 && h >= 320) return true;
                  }
                  return false;
                }"""
            )
        )
    except Exception:
        return False


async def _open_fresh_ai_image_composer(
    page: Any,
    landing_url: str,
    *,
    force_landing: bool = False,
) -> tuple[Any, bool]:
    """매직 다시 그리기 없이 AI Image 입력 화면을 연다.

    ``force_landing=True`` — 실행 시작 시 랜딩(ai_image) 강제.
    ``False`` — 대화(agents?id=) 유지, 매직 편집 UI만 제거.
    """
    bare = _ai_image_bare_url(landing_url)
    changed = False
    on_magic = await _page_is_magic_redraw(page)
    cur_sc = _score_ai_image_url(page.url or "")
    cur_bare = (page.url or "").split("#")[0].split("?")[0].rstrip("/")
    need_goto = (
        on_magic
        or force_landing
        or (cur_sc < 40 and cur_bare != bare.rstrip("/"))
    )
    if need_goto:
        _tab_log(
            f"AI Image 입력창 새로 열기 — "
            f"magic={on_magic} force={force_landing} "
            f"cur={(page.url or '')[:80]}"
        )
        await page.goto(bare, wait_until="domcontentloaded", timeout=90_000)
        await page.wait_for_timeout(900)
        changed = True
    if await _page_is_magic_redraw(page):
        await page.reload(wait_until="domcontentloaded", timeout=90_000)
        await page.wait_for_timeout(1000)
        changed = True
    await _install_page_guards(page)
    if await _page_is_magic_redraw(page):
        _tab_log("매직 다시 그리기 잔존 — reload 후에도 편집 UI")
    elif changed:
        _tab_log(f"AI Image 입력창 준비 → {(page.url or '')[:100]}")
    return page, changed


async def _exit_magic_redraw(
    page: Any, landing_url: str
) -> tuple[Any, bool]:
    """매직 다시 그리기 → AI Image 프롬프트 입력 화면 (폴백)."""
    if not await _page_is_magic_redraw(page):
        return page, False
    page, changed = await _open_fresh_ai_image_composer(page, landing_url)
    if not await _page_is_magic_redraw(page):
        return page, True
    _tab_log("매직 다시 그리기 — fresh composer 후에도 잔존")
    return page, changed


async def _soft_exit_magic_redraw(page: Any) -> tuple[Any, bool]:
    """prepared 입력 보존 — reload 만, goto 로 입력창 비우지 않음."""
    if not await _page_is_magic_redraw(page):
        return page, False
    _tab_log("매직 편집 UI — prepared 보존 reload 시도")
    try:
        await page.reload(wait_until="domcontentloaded", timeout=90_000)
        await page.wait_for_timeout(900)
        await _install_page_guards(page)
    except Exception as ex:
        _tab_log(f"매직 soft reload 예외: {ex}")
        return page, False
    if not await _page_is_magic_redraw(page):
        _tab_log("매직 편집 UI — reload 후 composer 복귀")
        return page, True
    _tab_log("매직 편집 UI — prepared 보존을 위해 goto 생략")
    return page, False


async def _ensure_composer_page(
    page: Any,
    landing_url: str,
    *,
    work_url: str = "",
    fresh: bool = False,
    preserve_input: bool = False,
) -> tuple[Any, bool]:
    """동영상 에이전트·매직 다시 그리기 등 입력 불가 UI 에서 벗어난다.

    ``preserve_input=True`` — goto 로 붙여넣은 대용량 입력을 지우지 않는다.
    """
    changed = False
    page, fix = await _redirect_from_wrong_agent(
        page, landing_url, work_url=work_url
    )
    changed = changed or fix
    on_magic = await _page_is_magic_redraw(page)
    if fresh and not preserve_input:
        page, fix = await _open_fresh_ai_image_composer(page, landing_url)
        changed = changed or fix
    elif on_magic:
        if preserve_input:
            page, fix = await _soft_exit_magic_redraw(page)
            changed = changed or fix
        else:
            page, fix = await _open_fresh_ai_image_composer(page, landing_url)
            changed = changed or fix
    elif not preserve_input:
        page, fix = await _exit_magic_redraw(page, landing_url)
        changed = changed or fix
    return page, changed or fix


async def _redirect_from_wrong_agent(
    page: Any,
    landing_url: str,
    *,
    work_url: str = "",
) -> tuple[Any, bool]:
    """동영상 등 잘못된 에이전트 탭이면 AI Image·저장 대화로 복귀."""
    cur = (page.url or "").strip()
    if not _is_wrong_agent_url(cur):
        return page, False
    _tab_log(f"AI Image 복귀 — 잘못된 URL {cur[:100]}")
    prefer = (work_url or "").strip()
    if (
        prefer
        and not _is_wrong_agent_url(prefer)
        and _score_ai_image_url(prefer) >= 40
    ):
        target = prefer
    else:
        target = landing_url
    await page.goto(target, wait_until="domcontentloaded", timeout=90_000)
    await page.wait_for_timeout(800)
    await _install_same_tab_guards(page)
    _tab_log(f"AI Image 복귀 완료 → {(page.url or '')[:100]}")
    return page, True


async def _ensure_ai_image_page(
    page: Any, url: str, *, prefer_url: str = ""
) -> bool:
    """동일 탭 유지. agents 대화(id=)가 있으면 랜딩 ai_image 로 돌아가지 않는다."""
    cur = (page.url or "").strip()
    prefer = (prefer_url or "").strip()
    if _is_wrong_agent_url(cur):
        await _redirect_from_wrong_agent(page, url, work_url=prefer)
        return True
    page, redraw_fix = await _exit_magic_redraw(page, url)
    if redraw_fix:
        return True
    cur = (page.url or "").strip()
    cur_sc = _score_ai_image_url(cur)
    prefer_sc = _score_ai_image_url(prefer)
    _tab_log(
        f"ensure_page cur_sc={cur_sc} prefer_sc={prefer_sc} "
        f"cur={(cur or '')[:100]} prefer={(prefer or '')[:100]}"
    )
    # 이미 대화 세션(agents?id= 등)이면 그대로
    if cur_sc >= 40:
        return False
    # 저장된 대화 URL이 더 좋으면 복귀
    if prefer_sc >= 40 and prefer_sc > cur_sc:
        await page.goto(prefer, wait_until="domcontentloaded", timeout=90_000)
        await page.wait_for_timeout(800)
        await _install_same_tab_guards(page)
        return True
    if prefer_sc >= 30 and prefer_sc > cur_sc:
        await page.goto(prefer, wait_until="domcontentloaded", timeout=90_000)
        await page.wait_for_timeout(800)
        await _install_same_tab_guards(page)
        return True
    # 랜딩(ai_image) — 매직 다시 그리기 등 편집 UI 면 입력창으로 복귀
    if cur_sc >= 10:
        page, redraw_fix = await _exit_magic_redraw(page, url)
        return redraw_fix
    await page.goto(url, wait_until="domcontentloaded", timeout=90_000)
    await page.wait_for_timeout(1500)
    await _install_same_tab_guards(page)
    return True


async def _ensure_generation_session(
    page: Any,
    context: Any,
    *,
    url: str,
    work_url: str,
    email: str = "",
    password: str = "",
    model_selector: str = "",
    model_texts: tuple[str, ...] | None = None,
    model_ready: bool = False,
) -> tuple[Any, str, bool]:
    """이어쓰기·첨부 가능한 AI Image 대화 URL까지 맞춘다."""
    page = await _alive_page(context, page)
    page, _ = await _ensure_composer_page(
        page, url, work_url=work_url or ""
    )
    cur_sc = _score_ai_image_url(page.url or "")
    if cur_sc >= 40:
        return page, _maybe_set_work_url(work_url, page.url or ""), model_ready

    prefer = (work_url or "").strip()
    prefer_sc = _score_ai_image_url(prefer)
    if prefer and prefer_sc >= 40 and prefer_sc > cur_sc:
        await page.goto(prefer, wait_until="domcontentloaded", timeout=90_000)
        await page.wait_for_timeout(800)
        page = await _alive_page(context, page)
        await _install_same_tab_guards(page)
        if _score_ai_image_url(page.url or "") >= 40:
            w = _maybe_set_work_url(work_url, page.url or "")
            _tab_log(f"세션복귀 sc={_score_ai_image_url(page.url or '')}")
            return page, w, model_ready

    await _ensure_ai_image_page(page, url, prefer_url=prefer)
    page = await _alive_page(context, page)
    if _score_ai_image_url(page.url or "") >= 40:
        return page, _maybe_set_work_url(work_url, page.url or ""), model_ready

    if "genspark.ai" not in (page.url or "").lower():
        await page.goto(url, wait_until="domcontentloaded", timeout=90_000)
        await page.wait_for_timeout(1200)
        page = await _alive_page(context, page)
    if email and password and not await _is_logged_in(page):
        await _ensure_login(
            page, email, password, context=context, force=False
        )
    if not model_ready:
        model_ready = await _select_nano_banana_pro(
            page,
            custom_selector=model_selector,
            model_texts=model_texts,
        )
    page = await _alive_page(context, page)
    w = _maybe_set_work_url(work_url, page.url or "")
    _tab_log(
        f"세션준비 sc={_score_ai_image_url(page.url or '')} "
        f"url={(page.url or '')[:90]}"
    )
    return page, w, model_ready


def _score_ai_image_url(url: str) -> int:
    """대화/결과 탭일수록 높은 점수. 랜딩·로그인은 낮음.

    Genspark 이미지 생성은 제출 후 ``/agents?id=…`` 로 이동한다.
    이 URL을 낮게 보면 이어쓰기 때 랜딩으로 돌아가 새 창·새 대화가 열린다.
    """
    u = (url or "").lower()
    if not u or "genspark" not in u:
        return -100
    if "accounts.google" in u:
        return -100
    if _is_wrong_agent_url(u):
        return -100
    # 활성 대화 세션 (이어쓰기 대상)
    if "/agents" in u and "id=" in u:
        return 80
    if "/agents" in u and "image_generation" in u:
        if "chat_now" in u or "action=" in u:
            return 55
        return 35
    if "ai_image" not in u and "ai-image" not in u:
        return 0
    bare = u.split("?")[0].rstrip("/")
    if bare.endswith("/ai_image") or bare.endswith("/ai-image"):
        return 10  # 랜딩
    return 40  # ai_image + 쿼리(구형 대화)


def _page_ctx_label(url: str) -> str:
    """image.log 점검용 — 첨부·전송 시 랜딩 vs 대화 URL 구분."""
    sc = _score_ai_image_url(url or "")
    u = (url or "").strip()
    if _is_wrong_agent_url(u):
        kind = "잘못된에이전트(video 등)"
    elif "매직" in u or "redraw" in u.lower():
        kind = "매직다시그리기(의심)"
    elif sc >= 80:
        kind = "대화(agents)"
    elif sc >= 40:
        kind = "대화"
    elif sc >= 10:
        kind = "랜딩(ai_image)"
    else:
        kind = "기타"
    short = u[:90] + ("…" if len(u) > 90 else "")
    return f"sc={sc} {kind} · {short}"


async def _install_same_tab_guards(page: Any) -> None:
    """새 창/새 탭 열기 금지 — 항상 현재 탭에서만 이동."""
    try:
        await page.add_init_script(_SAME_TAB_SCRIPT)
    except Exception:
        pass
    try:
        await page.evaluate(_SAME_TAB_SCRIPT)
    except Exception:
        pass


async def _install_anti_magic_redraw_guards(page: Any) -> None:
    """결과 이미지 클릭 등으로 매직 다시 그리기 UI 진입 차단."""
    try:
        await page.add_init_script(_ANTI_MAGIC_REDRAW_SCRIPT)
    except Exception:
        pass
    try:
        await page.evaluate(_ANTI_MAGIC_REDRAW_SCRIPT)
    except Exception:
        pass


async def _install_page_guards(page: Any) -> None:
    await _install_same_tab_guards(page)
    await _install_anti_magic_redraw_guards(page)


async def _install_context_same_tab_guards(context: Any) -> None:
    try:
        await context.add_init_script(_SAME_TAB_SCRIPT)
        await context.add_init_script(_ANTI_MAGIC_REDRAW_SCRIPT)
    except Exception:
        pass


async def _leave_single_tab(context: Any, url: str, page: Any | None = None) -> Any:
    """탭을 1개만 남기고 url로 연다. 「실행」용."""
    pages = [p for p in list(getattr(context, "pages", []) or []) if not p.is_closed()]
    keep = page if page is not None and not page.is_closed() else (pages[0] if pages else None)
    if keep is None:
        keep = await context.new_page()
    for p in list(getattr(context, "pages", []) or []):
        if p is keep:
            continue
        try:
            if not p.is_closed():
                await p.close()
        except Exception:
            pass
    keep = (await _open_fresh_ai_image_composer(keep, url, force_landing=True))[0]
    return keep


def _context_has_live_page(context: Any) -> bool:
    try:
        for p in list(getattr(context, "pages", []) or []):
            try:
                if not p.is_closed():
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


async def _alive_page(context: Any, page: Any) -> Any:
    """닫힌 page 참조를 복구. 동일 탭(Genspark)을 우선한다."""
    try:
        if page is not None and not page.is_closed():
            return page
    except Exception:
        pass
    pages = list(getattr(context, "pages", []) or [])
    best = None
    best_sc = -999
    for p in pages:
        try:
            if p.is_closed():
                continue
            sc = _score_ai_image_url(p.url or "")
            if sc > best_sc:
                best_sc = sc
                best = p
        except Exception:
            continue
    if best is not None:
        await _install_same_tab_guards(best)
        try:
            await best.bring_to_front()
        except Exception:
            pass
        return best
    raise BrowserClosedError(BROWSER_CLOSED_MSG)


async def _close_all_extra_tabs(context: Any, keep: Any) -> None:
    """keep 이외 탭만 닫는다. keep가 유일한 탭이면 아무 것도 안 함."""
    try:
        alive = [p for p in list(context.pages or []) if not p.is_closed()]
    except Exception:
        return
    if len(alive) <= 1:
        return
    keep_id = id(keep) if keep is not None else None
    closed_n = 0
    for p in alive:
        if keep is not None and (p is keep or id(p) == keep_id):
            continue
        try:
            if p.is_closed():
                continue
            pu = (p.url or "").lower()
            if "accounts.google" in pu:
                continue
            _tab_log(f"탭닫기(여분) url={(p.url or '')[:100]}")
            await p.close()
            closed_n += 1
        except Exception as ex:
            _tab_log(f"탭닫기 실패: {ex}")
    if closed_n:
        _tab_log(f"여분 탭 닫음 n={closed_n} · {await _tab_snapshot(context, keep)}")


async def _adopt_same_tab(
    context: Any, page: Any, *, navigate: bool = True
) -> Any:
    """여분 탭을 정리. ``navigate=True`` 이면 더 좋은 Genspark URL로 작업 탭 이동.

    생성 대기 중에는 ``navigate=False`` 로 호출해 작업 탭 네비게이션을 막는다.
    """
    page = await _alive_page(context, page)
    await _install_same_tab_guards(page)
    before = await _tab_snapshot(context, page)

    page_score = _score_ai_image_url(page.url or "")
    best_url = ""
    best_score = -999
    best_page = None
    for p in list(getattr(context, "pages", []) or []):
        if p is page:
            continue
        try:
            if p.is_closed():
                continue
            u = (p.url or "").strip()
            if not u or u.startswith("about:") or u.startswith("chrome:"):
                continue
            sc = _score_ai_image_url(u)
            if "genspark" in u.lower() and sc >= 10 and sc >= page_score and sc >= best_score:
                best_score = sc
                best_url = u
                best_page = p
        except Exception:
            continue

    if best_page is not None and best_score > page_score:
        # 새 탭이 더 좋은 대화면 그 탭을 작업 탭으로 승격 (goto 로 기존 탭 깨지 않음)
        _tab_log(
            f"adopt: 작업탭 승격 score {page_score}→{best_score} url={best_url[:100]}"
        )
        page = best_page
        await _install_same_tab_guards(page)
    elif navigate and best_url:
        try:
            await page.bring_to_front()
            if (page.url or "").rstrip("/") != best_url.rstrip("/"):
                _tab_log(f"adopt: goto {best_url[:100]}")
                await page.goto(best_url, wait_until="domcontentloaded", timeout=90_000)
                await page.wait_for_timeout(300)
        except Exception as ex:
            _tab_log(f"adopt: goto 실패 {ex}")
            page = await _alive_page(context, page)

    await _close_all_extra_tabs(context, page)
    page = await _alive_page(context, page)
    try:
        await page.bring_to_front()
    except Exception:
        pass
    await _install_same_tab_guards(page)
    after = await _tab_snapshot(context, page)
    if before != after:
        _tab_log(f"adopt 후 · {after}")
    return page


async def _merge_popup_into_page(
    context: Any, page: Any, popup: Any, *, allow_navigate: bool = True
) -> Any:
    """새 탭 URL만 기존 탭으로 옮기고 팝업을 닫는다. 기존 page를 닫지 않음."""
    # 빈/약:blank 팝업은 그냥 닫기
    try:
        if popup is None or popup.is_closed():
            return await _alive_page(context, page)
    except Exception:
        return await _alive_page(context, page)

    try:
        if page is not None and not page.is_closed() and popup is page:
            return page
    except Exception:
        pass

    dest = ""
    for _ in range(16):
        try:
            if popup.is_closed():
                break
            u = (popup.url or "").strip()
            if u.startswith("about:") or u.startswith("chrome:"):
                await asyncio.sleep(0.2)
                continue
            if u and "genspark" in u.lower():
                dest = u
                break
            if u and "genspark" not in u.lower() and "google" not in u.lower():
                break
        except Exception:
            break
        await asyncio.sleep(0.15)

    if dest and _is_wrong_agent_url(dest):
        _tab_log(f"merge: 잘못된 에이전트 팝업 닫기 url={dest[:100]}")
        try:
            if not popup.is_closed() and popup is not page:
                await popup.close()
        except Exception:
            pass
        return await _alive_page(context, page)

    popup_score = _score_ai_image_url(dest) if dest else -100
    page_alive = page is not None and not page.is_closed()
    page_score = _score_ai_image_url(page.url or "") if page_alive else -100
    _tab_log(
        f"merge: popup={(dest or (getattr(popup, 'url', '') or ''))[:100]} "
        f"score={popup_score} page_score={page_score} nav={allow_navigate} · "
        f"{await _tab_snapshot(context, page)}"
    )

    # 팝업이 더 좋은 대화면 작업 탭을 팝업으로 바꾸고 예전 탭만 닫기
    if dest and popup_score > page_score and not popup.is_closed():
        old = page
        page = popup
        _tab_log(f"merge: 작업탭=새탭 승격 (구탭 닫기)")
        try:
            if old is not None and old is not page and not old.is_closed():
                await old.close()
        except Exception:
            pass
        await _install_same_tab_guards(page)
        await _close_all_extra_tabs(context, page)
        return await _alive_page(context, page)

    # 팝업만 닫기 (page 유지)
    try:
        if not popup.is_closed() and popup is not page:
            await popup.close()
            _tab_log("merge: 팝업 닫음")
    except Exception as ex:
        _tab_log(f"merge: 팝업 닫기 실패 {ex}")

    page = await _alive_page(context, page)
    if allow_navigate and dest and "genspark" in dest.lower():
        try:
            await page.bring_to_front()
            if (page.url or "").rstrip("/") != dest.rstrip("/"):
                _tab_log(f"merge: page.goto {dest[:100]}")
                await page.goto(dest, wait_until="domcontentloaded", timeout=90_000)
                await page.wait_for_timeout(250)
        except Exception as ex:
            _tab_log(f"merge: goto 실패 {ex}")
            page = await _alive_page(context, page)
    await _close_all_extra_tabs(context, page)
    page = await _alive_page(context, page)
    await _install_same_tab_guards(page)
    try:
        await page.bring_to_front()
    except Exception:
        pass
    return page


async def _collect_images(page: Any) -> list[tuple[int | None, str]]:
    try:
        await page.evaluate(
            """() => {
              window.scrollTo(0, 0);
              const h = document.body && document.body.scrollHeight || 0;
              window.scrollTo(0, Math.max(0, h - 400));
            }"""
        )
        await page.wait_for_timeout(800)
    except Exception:
        pass
    raw = await page.evaluate(
        """() => {
          const out = [];
          const seen = new Set();
          const skipParts = [
            'bat.bing.com', 'bing.com/action', 'google-analytics.com',
            'googletagmanager.com', 'doubleclick.net', 'clarity.ms', 'hotjar.com'
          ];
          const goodHostParts = [
            'genspark', 'cloudinary', 'amazonaws.com', 'googleusercontent.com',
            'openai.com', 'oaidalle', 'blob.core.windows.net'
          ];
          const isSkip = (url) => {
            if (!url) return true;
            const u = url.toLowerCase();
            if (u.startsWith('data:')) return true;
            if (u.includes('/action/0?') && u.includes('bing')) return true;
            return skipParts.some(p => u.includes(p));
          };
          const looksImage = (url) => {
            if (!url) return false;
            if (url.startsWith('blob:')) return true;
            if (url.toLowerCase().includes('www.genspark.ai/api/files')) return true;
            if (/\\.(png|jpe?g|webp|gif|avif)(\\?|$|#)/i.test(url)) return true;
            try {
              const host = new URL(url).hostname.toLowerCase();
              return goodHostParts.some(p => host.includes(p));
            } catch (e) { return false; }
          };
          const push = (url, label, w, h) => {
            if (!url || seen.has(url) || isSkip(url)) return;
            const isFile = url.toLowerCase().includes('www.genspark.ai/api/files');
            if (!isFile && !looksImage(url) && (w < 256 || h < 256)) return;
            if (!isFile && !url.startsWith('blob:') && (!w || !h) && !looksImage(url)) return;
            seen.add(url);
            out.push({url, label: label || '', w, h});
          };
          for (const img of document.querySelectorAll('img')) {
            const w = img.naturalWidth || img.width || 0;
            const h = img.naturalHeight || img.height || 0;
            const src = img.currentSrc || img.src || '';
            let label = '';
            const near = (img.closest('figure,article,div,li,section') || img.parentElement);
            if (near) label = (near.innerText || '').slice(0, 800);
            push(src, (label + ' ' + (img.alt || '')).trim(), w, h);
            const ds = img.getAttribute('data-src') || img.getAttribute('data-original') || '';
            if (ds) push(ds, label, w, h);
          }
          for (const a of document.querySelectorAll('a[href]')) {
            const href = a.href || '';
            if (!looksImage(href) && !/download/i.test(href)) continue;
            push(href, ((a.innerText || '') + ' ' + (a.getAttribute('download') || '')).trim(), 0, 0);
          }
          return out;
        }"""
    )
    items: list[tuple[int | None, str]] = []
    for row in raw or []:
        if not isinstance(row, dict):
            continue
        url = str(row.get("url") or "").strip()
        label = str(row.get("label") or "")
        w = int(row.get("w") or 0)
        h = int(row.get("h") or 0)
        if not url or is_tracking_url(url):
            continue
        if not url.startswith("blob:") and not is_collectable_image_url(
            url, width=w, height=h
        ):
            continue
        sec = None
        m = _SRT_LABEL_RE.search(label) or _SRT_LABEL_RE.search(url)
        if m:
            sec = int(m.group(1))
        items.append((sec, url))
    return items


async def _launch_context(playwright: Any, profile_dir: Path) -> Any:
    # 1) 이 슬롯 Chrome(CDP)에만 연결 — 다른 인스턴스 포트는 보지 않음
    del profile_dir  # CDP 연결 시 Playwright 프로필 미사용
    port, _ud = _slot_defaults()
    for _ in range(60):
        try:
            browser = await playwright.chromium.connect_over_cdp(
                f"http://127.0.0.1:{port}"
            )
            if browser.contexts:
                return browser.contexts[0]
        except Exception:
            pass
        await asyncio.sleep(0.5)

    raise RuntimeError(
        f"ChromeDebug(CDP :{port})에 연결하지 못했습니다.\n"
        "「실행」로 ChromeDebug를 먼저 띄운 뒤 다시 시도하세요."
    )


class GensparkSceneSession:
    """Playwright 세션 — Nano banana pro · 씬 프롬프트 전송·이미지 수집."""

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

    def open_and_select_model(
        self,
        *,
        url: str,
        model_selector: str = "",
        email: str = "",
        password: str = "",
        model_texts: tuple[str, ...] | list[str] | None = None,
        timeout_sec: float = 420.0,
        light: bool = False,
    ) -> dict[str, bool]:
        """``light=True`` — script 개별: 기존 탭 유지·랜딩 강제 없이 로그인·모델만."""
        return self._call(
            "open_model",
            {
                "url": url,
                "model_selector": model_selector,
                "email": email,
                "password": password,
                "model_texts": list(model_texts or []),
                "light": bool(light),
            },
            timeout=float(max(240.0, timeout_sec)),
        )

    def ensure_fresh_composer(self, *, url: str) -> dict[str, Any]:
        """매직 다시 그리기 없이 AI Image 입력 화면만 연다."""
        return self._call(
            "fresh_composer",
            {"url": url},
            timeout=90.0,
        )

    def probe_limit_reset(
        self,
        *,
        url: str = "",
        email: str = "",
        password: str = "",
    ) -> dict[str, Any]:
        """AI Image 페이지에서 한도·정상화(재설정) 시각을 읽는다.

        Returns:
            ``{"reset_at": "YYYY-MM-DDTHH:MM:SS"|None, "snippet": str, "is_limit": bool}``
        """
        return self._call(
            "probe_limit",
            {
                "url": url or GENSPARK_AI_IMAGE_URL,
                "email": email,
                "password": password,
            },
            timeout=180.0,
        )

    def paste_text(
        self,
        *,
        url: str,
        text: str,
        model_selector: str = "",
        try_model_select: bool = True,
        model_texts: tuple[str, ...] | list[str] | None = None,
        append: bool = False,
    ) -> dict[str, bool]:
        """입력창에만 붙여넣기 (전송하지 않음). ``append=True`` 면 기존 내용 뒤에 추가."""
        return self._call(
            "paste",
            {
                "url": url,
                "text": text,
                "model_selector": model_selector,
                "try_model": try_model_select,
                "model_texts": list(model_texts or []),
                "append": bool(append),
            },
            timeout=max(240.0, 60.0 + len(text) / 200.0),
        )

    def attach_files(
        self,
        *,
        url: str,
        files: list[str | Path],
        model_selector: str = "",
        try_model_select: bool = True,
        model_texts: tuple[str, ...] | list[str] | None = None,
    ) -> dict[str, bool]:
        """SRT·이미지프롬프트 파일을 페이지에 첨부."""
        return self._call(
            "attach",
            {
                "url": url,
                "files": [str(Path(p)) for p in files],
                "model_selector": model_selector,
                "try_model": try_model_select,
                "model_texts": list(model_texts or []),
            },
            timeout=240.0,
        )

    def submit_prompt(
        self,
        *,
        url: str,
        prompt: str,
        model_selector: str = "",
        try_model_select: bool = True,
        model_texts: tuple[str, ...] | list[str] | None = None,
    ) -> dict[str, bool]:
        return self._call(
            "submit",
            {
                "url": url,
                "prompt": prompt,
                "model_selector": model_selector,
                "try_model": try_model_select,
                "model_texts": list(model_texts or []),
            },
            timeout=240.0,
        )

    def collect_images(self, *, wait_ms: int = 3000) -> list[tuple[int | None, str]]:
        return self._call("collect", wait_ms, timeout=120.0)

    def download_via_page(
        self,
        items: list[tuple[int | None, str]],
        png_dir: Path,
        *,
        fallback_secs: list[int] | None = None,
        default_start_sec: int | None = None,
    ) -> list[str]:
        return self._call(
            "download",
            {
                "items": items,
                "png_dir": str(png_dir),
                "fallback_secs": list(fallback_secs or []),
                "default_start_sec": default_start_sec,
            },
            timeout=300.0,
        )

    def run_scene_with_retry(
        self,
        *,
        url: str,
        prompt: str,
        png_dir: Path,
        srt_sec: int,
        model_selector: str = "",
        model_texts: tuple[str, ...] | list[str] | None = None,
        try_model_select: bool = False,
        retry_count: int = 1,
        retry_wait_sec: int = 30,
        generate_timeout_sec: int = 120,
        use_existing_input: bool = False,
        srt_path: str | Path | None = None,
        interval_sec: int = 20,
        prompt_path: str | Path | None = None,
        attach_reference: bool = True,
        prior_secs: list[int] | None = None,
        email: str = "",
        password: str = "",
        force_regenerate: bool = False,
        next_scene_sec: int | None = None,
        scene_secs: list[int] | None = None,
    ) -> dict[str, Any]:
        """명령 전송 → 생성 완료 대기 → 다운로드.

        실패 시 재시도하지 않는다 (호출측에서 다음 씬으로 진행).
        ``use_existing_input=True`` 이면 입력창(SRT·프롬프트+명령)을 그대로 전송한다.
        ``attach_reference=True`` 이면 직전 슬롯 PNG를 Ctrl+V로 첨부한다.
        (``SRT_020`` → ``SRT_005`` · 그 외 ``SRT_{t-interval}``)
        """
        del retry_wait_sec  # 재시도 없음
        return self._call(
            "run_scene",
            {
                "url": url,
                "prompt": prompt,
                "png_dir": str(png_dir),
                "srt_sec": int(srt_sec),
                "model_selector": model_selector,
                "model_texts": list(model_texts or []),
                "try_model": try_model_select,
                "retry_count": 1,
                "retry_wait_sec": 0,
                "generate_timeout_sec": int(generate_timeout_sec),
                "use_existing_input": bool(use_existing_input),
                "srt_path": str(srt_path) if srt_path else "",
                "interval_sec": int(interval_sec),
                "prompt_path": str(prompt_path) if prompt_path else "",
                "attach_reference": bool(attach_reference),
                "prior_secs": [int(s) for s in (prior_secs or [])],
                "email": str(email or ""),
                "password": str(password or ""),
                "force_regenerate": bool(force_regenerate),
                "next_scene_sec": (
                    int(next_scene_sec) if next_scene_sec is not None else None
                ),
                "scene_secs": [int(s) for s in (scene_secs or [])],
            },
            timeout=float(max(300, generate_timeout_sec + 120)),
        )

    def salvage_pending(
        self,
        *,
        png_dir: Path,
        secs: list[int] | None = None,
    ) -> dict[str, Any]:
        """중간에 다운로드 실패한 SRT를 페이지에서 한 번 더 회수.

        반환: ``recovered``(개수), ``still_missing``(초 목록), ``saved``(경로 목록).
        """
        return self._call(
            "salvage",
            {
                "png_dir": str(png_dir),
                "secs": [int(s) for s in (secs or [])],
            },
            timeout=180.0,
        )

    async def _async_worker(self) -> None:
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            context = await _launch_context(pw, self._profile_dir)
            await _install_context_same_tab_guards(context)
            page = context.pages[0] if context.pages else await context.new_page()
            _attach_filechooser_guard(page)
            await _install_same_tab_guards(page)
            for p in list(context.pages or []):
                _attach_filechooser_guard(p)
            model_ready = False
            work_url = ""
            # 붙여넣기·생성 중에는 새 탭 합치기 잠시 보류 (작업 탭 오닫힘 방지)
            merge_pause = {"v": False}
            # 이미 받은 genspark.ai/api/files/s/… → 파일명 (중복 저장 방지)
            seen_file_urls: dict[str, str] = {}
            last_saved_file_url = ""
            pending_fail_secs: list[int] = []
            char_state_tracker: Any | None = None
            try:

                def _on_new_page(p: Any) -> None:
                    # 새 탭/창 — URL·개수 로그 후, 생성 중이면 합치기 보류
                    _attach_filechooser_guard(p)

                    async def _merge() -> None:
                        nonlocal page
                        try:
                            u0 = ""
                            try:
                                u0 = (p.url or "")[:120]
                            except Exception:
                                u0 = "?"
                            _tab_log(
                                f"NEW_PAGE pause={merge_pause['v']} url={u0} · "
                                f"{await _tab_snapshot(context, page)}"
                            )
                        except Exception:
                            pass
                        if merge_pause["v"]:
                            # 생성·첨부 중: 새 탭 승격 금지 — 작업 탭만 유지
                            try:
                                await asyncio.sleep(0.25)
                                if p.is_closed():
                                    return
                                keep = (
                                    _ATTACH_KEEP_PAGE.get("page") or page
                                )
                                wu = (_ATTACH_WORK_URL.get("v") or "").strip()
                                aid = _agent_id_from_url(wu)
                                if aid and aid in (p.url or ""):
                                    keep = p
                                u0 = (p.url or "")[:80]
                                if p is not keep and not p.is_closed():
                                    await p.close()
                                    _tab_log(
                                        f"NEW_PAGE(pause): 여분 탭 닫음 "
                                        f"url={u0}"
                                    )
                                if keep and not keep.is_closed():
                                    page = keep
                                    await keep.bring_to_front()
                            except Exception as ex:
                                _tab_log(f"NEW_PAGE(pause) 예외: {ex}")
                            return
                        try:
                            page = await _merge_popup_into_page(
                                context, page, p, allow_navigate=True
                            )
                            _tab_log(
                                f"NEW_PAGE: merge 완료 · "
                                f"{await _tab_snapshot(context, page)}"
                            )
                        except Exception as ex:
                            _tab_log(f"NEW_PAGE merge 실패: {ex}")
                            try:
                                if p is not page and not p.is_closed():
                                    await p.close()
                            except Exception:
                                pass
                            try:
                                page = await _alive_page(context, page)
                            except Exception:
                                pass

                    asyncio.create_task(_merge())

                context.on("page", _on_new_page)
            except Exception:
                pass

            while True:
                try:
                    op, arg, resp_q = self._cmd_q.get_nowait()
                except queue.Empty:
                    await asyncio.sleep(0.15)
                    continue
                try:
                    if op == "fresh_composer":
                        data = arg or {}
                        url = data.get("url") or GENSPARK_AI_IMAGE_URL
                        page = await _alive_page(context, page)
                        page, fresh = await _open_fresh_ai_image_composer(
                            page, url, force_landing=False
                        )
                        resp_q.put(
                            (
                                True,
                                {"fresh": bool(fresh), "url": page.url or ""},
                                None,
                            )
                        )
                    elif op == "open_model":
                        data = arg or {}
                        url = data.get("url") or GENSPARK_AI_IMAGE_URL
                        email = str(data.get("email") or "")
                        password = str(data.get("password") or "")
                        mtexts = tuple(data.get("model_texts") or []) or None
                        light = bool(data.get("light"))
                        merge_pause["v"] = True
                        page = await _alive_page(context, page)
                        if light:
                            page, _ = await _open_fresh_ai_image_composer(
                                page, url, force_landing=False
                            )
                            _tab_log("open_model(light): composer 준비")
                        else:
                            # 기존 탭 전부 닫고 새 탭 1개만
                            page = await _leave_single_tab(context, url, page)
                            page = await _alive_page(context, page)
                            page, _ = await _open_fresh_ai_image_composer(
                                page, url, force_landing=True
                            )
                            _tab_log("open_model: AI Image 랜딩 완료")
                        work_url = page.url or url
                        await _install_page_guards(page)
                        merge_pause["v"] = False
                        # storageState/쿠키로 로그인 유지 — 만료 시에만 재로그인
                        login_info = await _ensure_login(
                            page,
                            email,
                            password,
                            context=context,
                            force=False,
                        )
                        if "genspark.ai" not in (page.url or "").lower() or (
                            _score_ai_image_url(page.url or "") < 10
                        ):
                            await page.goto(
                                url,
                                wait_until="domcontentloaded",
                                timeout=90_000,
                            )
                            await page.wait_for_timeout(1500)
                        if email and password and not await _is_logged_in(page):
                            login_info = await _ensure_login(
                                page,
                                email,
                                password,
                                context=context,
                                force=True,
                            )
                            if login_info.get("logged_in"):
                                await _save_storage_state(
                                    context,
                                    storage_state_path(self._profile_dir),
                                )
                            if _score_ai_image_url(page.url or "") < 10:
                                await page.goto(
                                    url,
                                    wait_until="domcontentloaded",
                                    timeout=90_000,
                                )
                                await page.wait_for_timeout(1200)
                        elif login_info.get("logged_in"):
                            await _save_storage_state(
                                context,
                                storage_state_path(self._profile_dir),
                            )
                        _tab_log(
                            f"open_model: login "
                            f"logged_in={bool(login_info.get('logged_in'))} "
                            f"attempted={bool(login_info.get('attempted'))}"
                        )
                        model_auto = await _select_nano_banana_pro(
                            page,
                            custom_selector=str(data.get("model_selector") or ""),
                            model_texts=mtexts,
                        )
                        model_ready = model_auto
                        _tab_log(f"open_model: model_auto={bool(model_auto)}")
                        page = await _adopt_same_tab(context, page)
                        work_url = page.url or work_url or url
                        resp_q.put(
                            (
                                True,
                                {
                                    "model_auto": bool(model_auto),
                                    "logged_in": bool(
                                        login_info.get("logged_in")
                                    ),
                                    "login_attempted": bool(
                                        login_info.get("attempted")
                                    ),
                                    "login_filled": bool(
                                        login_info.get("filled")
                                    ),
                                },
                                None,
                            )
                        )
                    elif op == "paste":
                        data = arg or {}
                        url = data.get("url") or GENSPARK_AI_IMAGE_URL
                        mtexts = tuple(data.get("model_texts") or []) or None
                        merge_pause["v"] = True
                        try:
                            page = await _alive_page(context, page)
                            if await _ensure_ai_image_page(
                                page, url, prefer_url=work_url or ""
                            ):
                                model_ready = False
                            page = await _alive_page(context, page)
                            text = (
                                data.get("text") or data.get("prompt") or ""
                            ).strip()
                            if not text:
                                raise RuntimeError("붙여넣을 텍스트가 비어 있습니다.")
                            large_paste = len(text) > 5000 and not data.get(
                                "append"
                            )
                            page, _ = await _ensure_composer_page(
                                page,
                                url,
                                work_url=work_url or "",
                                preserve_input=large_paste,
                            )
                            model_auto = model_ready
                            if data.get("try_model") and not model_ready:
                                model_auto = await _select_nano_banana_pro(
                                    page,
                                    custom_selector=str(
                                        data.get("model_selector") or ""
                                    ),
                                    model_texts=mtexts,
                                )
                                model_ready = model_auto
                            # 랜딩 입력란 로드 대기
                            if not await _wait_editor_ready(page, timeout_sec=60.0):
                                _tab_log("paste: 입력란 대기 초과 — raw 재시도")
                                await page.wait_for_timeout(1500)
                            if data.get("append"):
                                ok = await _append_to_first_editable(page, text)
                            else:
                                follow = _is_followup_command(text)
                                ok = await _fill_first_editable(
                                    page, text, prefer_followup=follow
                                )
                            if not ok:
                                # 한 번 더: 페이지 새로고침 없이 raw 폴백만
                                raw = await _raw_visible_editors(page)
                                if raw:
                                    try:
                                        await raw[0].click(timeout=3000)
                                        if len(text) > 8_000:
                                            await page.evaluate(
                                                """async (t) => {
                                                  await navigator.clipboard.writeText(t);
                                                }""",
                                                text,
                                            )
                                            await page.keyboard.press("Control+A")
                                            await page.keyboard.press("Control+V")
                                        else:
                                            await page.keyboard.press("Control+A")
                                            await page.keyboard.insert_text(text)
                                        ok = True
                                        _tab_log("paste: raw 폴백 성공")
                                    except Exception as ex:
                                        _tab_log(f"paste: raw 폴백 실패 {ex}")
                            if not ok:
                                raise RuntimeError(
                                    "명령어 입력란을 찾지 못했습니다. "
                                    "로그인·페이지 로드 후 다시 시도하세요."
                                )
                            page = await _alive_page(context, page)
                            await page.wait_for_timeout(400)
                            resp_q.put((True, {"model_auto": bool(model_auto)}, None))
                        finally:
                            merge_pause["v"] = False
                    elif op == "attach":
                        data = arg or {}
                        url = data.get("url") or GENSPARK_AI_IMAGE_URL
                        mtexts = tuple(data.get("model_texts") or []) or None
                        files = [
                            Path(p)
                            for p in (data.get("files") or [])
                            if Path(p).is_file()
                        ]
                        if not files:
                            raise RuntimeError("첨부할 파일이 없습니다.")
                        if await _ensure_ai_image_page(
                            page, url, prefer_url=work_url or ""
                        ):
                            model_ready = False
                            await page.wait_for_timeout(500)
                        model_auto = model_ready
                        if data.get("try_model") and not model_ready:
                            model_auto = await _select_nano_banana_pro(
                                page,
                                custom_selector=str(
                                    data.get("model_selector") or ""
                                ),
                                model_texts=mtexts,
                            )
                            model_ready = model_auto
                        ok = await _attach_files(page, files)
                        page, wrong_fix = await _ensure_composer_page(
                            page, url, work_url=work_url or ""
                        )
                        if wrong_fix and ok:
                            ok = await _attach_files(page, files)
                        if not ok:
                            raise RuntimeError(
                                "파일 첨부 UI를 찾지 못했습니다. "
                                "페이지 로드·로그인 후 다시 「실행」하세요."
                            )
                        await page.wait_for_timeout(800)
                        resp_q.put(
                            (
                                True,
                                {
                                    "model_auto": bool(model_auto),
                                    "attached": True,
                                    "n_files": len(files),
                                },
                                None,
                            )
                        )
                    elif op == "submit":
                        data = arg or {}
                        url = data.get("url") or GENSPARK_AI_IMAGE_URL
                        mtexts = tuple(data.get("model_texts") or []) or None
                        page = await _adopt_same_tab(context, page)
                        if await _ensure_ai_image_page(
                            page, url, prefer_url=work_url or ""
                        ):
                            model_ready = False
                        model_auto = model_ready
                        if data.get("try_model") and not model_ready:
                            model_auto = await _select_nano_banana_pro(
                                page,
                                custom_selector=str(
                                    data.get("model_selector") or ""
                                ),
                                model_texts=mtexts,
                            )
                            model_ready = model_auto
                        prompt = (data.get("prompt") or "").strip()
                        if not prompt:
                            raise RuntimeError("프롬프트가 비어 있습니다.")
                        follow = _is_followup_command(prompt)
                        if not await _fill_first_editable(
                            page, prompt, prefer_followup=follow
                        ):
                            raise RuntimeError(
                                "명령어 입력란을 찾지 못했습니다. "
                                "로그인·페이지 로드 후 다시 시도하세요."
                            )
                        await _install_same_tab_guards(page)
                        await _ensure_submitted(page)
                        await page.wait_for_timeout(400)
                        page = await _adopt_same_tab(context, page)
                        await _close_all_extra_tabs(context, page)
                        if page.url and _score_ai_image_url(page.url) >= 35:
                            work_url = _maybe_set_work_url(work_url, page.url)
                        resp_q.put((True, {"model_auto": bool(model_auto)}, None))
                    elif op == "run_scene":
                        data = arg or {}
                        url = data.get("url") or GENSPARK_AI_IMAGE_URL
                        prompt_raw = (data.get("prompt") or "").strip()
                        png_dir = Path(data.get("png_dir") or ".")
                        srt_sec = int(data.get("srt_sec") or 0)
                        srt_path = (data.get("srt_path") or "").strip() or None
                        prompt_path = (data.get("prompt_path") or "").strip() or None
                        interval_sec = max(1, int(data.get("interval_sec") or 20))
                        _raw_next = data.get("next_scene_sec")
                        next_scene_sec = (
                            int(_raw_next)
                            if _raw_next is not None and str(_raw_next) != ""
                            else None
                        )
                        from scene_image.character_bible import get_registry
                        from scene_image.character_consistency import CharacterStateTracker

                        if char_state_tracker is None:
                            reg = get_registry(
                                prompt_path=prompt_path, png_dir=png_dir
                            )
                            char_state_tracker = CharacterStateTracker.load(
                                png_dir, registry=reg
                            )
                        attach_reference = bool(data.get("attach_reference", True))
                        prior_secs: list[int] = []
                        for raw in data.get("prior_secs") or []:
                            try:
                                prior_secs.append(int(raw))
                            except (TypeError, ValueError):
                                continue
                        scene_secs_list: list[int] | None = None
                        _raw_scene_secs = data.get("scene_secs") or []
                        if _raw_scene_secs:
                            scene_secs_list = []
                            for raw in _raw_scene_secs:
                                try:
                                    scene_secs_list.append(int(raw))
                                except (TypeError, ValueError):
                                    continue
                            if not scene_secs_list:
                                scene_secs_list = None
                        mtexts = tuple(data.get("model_texts") or []) or None
                        gen_timeout = max(120, int(data.get("generate_timeout_sec") or 120))
                        use_existing_input = bool(data.get("use_existing_input"))
                        # data retry_* 무시 — 재시도 없음
                        _ = data.get("retry_count")
                        _ = data.get("retry_wait_sec")
                        png_dir.mkdir(parents=True, exist_ok=True)
                        force_regenerate = bool(data.get("force_regenerate"))
                        # PNG 폴더에 이미 있으면 재생성하지 않음 (개별 재생성 제외)
                        if not force_regenerate and png_already_exists(
                            png_dir, srt_sec
                        ):
                            existing = png_dir / srt_png_name(srt_sec)
                            _LAST_SCENE_REF["sec"] = int(srt_sec)
                            _LAST_SCENE_REF["path"] = str(existing.resolve())
                            resp_q.put(
                                (
                                    True,
                                    {
                                        "ok": True,
                                        "skipped": True,
                                        "attempt": 0,
                                        "saved": [str(existing.resolve())],
                                    },
                                    None,
                                )
                            )
                            continue
                        last_err = ""
                        saved_paths: list[str] = []
                        set_tab_log_png_dir(png_dir)
                        # png 폴더에 기록된 기존 files URL 로드
                        seen_file_urls.update(_load_seen_file_urls(png_dir))
                        if not last_saved_file_url and seen_file_urls:
                            last_saved_file_url = next(reversed(seen_file_urls.keys()))
                        page = await _alive_page(context, page)
                        if pending_fail_secs:
                            late = await _salvage_late_images(
                                page,
                                png_dir,
                                pending_fail_secs,
                                seen_file_urls,
                            )
                            if late:
                                last_saved_file_url = late
                        _tab_log(
                            f"run_scene 시작 SRT_{srt_sec:03d} · "
                            f"seen_files={len(seen_file_urls)} · "
                            f"last_saved="
                            f"{normalize_genspark_file_url(last_saved_file_url)[:80]}"
                        )
                        # 재시도 없음 — 1회만 시도 후 실패 시 호출측이 다음 씬으로
                        retry_count = 1
                        retry_wait = 0
                        for attempt in range(1, retry_count + 1):
                            try:
                                # 제출~다운로드 끝까지 탭 합치기 보류
                                merge_pause["v"] = True
                                t_scene = time.perf_counter()
                                t_phase = t_scene
                                page = await _alive_page(context, page)
                                page, work_url, model_ready = (
                                    await _ensure_generation_session(
                                        page,
                                        context,
                                        url=url,
                                        work_url=work_url or "",
                                        email=str(data.get("email") or ""),
                                        password=str(data.get("password") or ""),
                                        model_selector=str(
                                            data.get("model_selector") or ""
                                        ),
                                        model_texts=mtexts,
                                        model_ready=model_ready,
                                    )
                                )
                                await _install_same_tab_guards(page)
                                if data.get("try_model") and not model_ready:
                                    model_ready = await _select_nano_banana_pro(
                                        page,
                                        custom_selector=str(
                                            data.get("model_selector") or ""
                                        ),
                                        model_texts=mtexts,
                                    )
                                fail_before = await _failure_count(page)
                                ref_attached = False
                                ref_label = ""
                                ref_path = None
                                attach_ref_path: Path | None = None
                                page_sc = _score_ai_image_url(page.url or "")
                                submit_prepared_only = (
                                    use_existing_input
                                    and attempt == 1
                                    and page_sc < 40
                                )
                                _tab_log(
                                    f"씬컨텍스트 SRT_{srt_sec:03d} · "
                                    f"{_page_ctx_label(page.url or '')} · "
                                    f"prepared={submit_prepared_only}"
                                )
                                gap = max(1, int(interval_sec))
                                watch_prior = (
                                    max(prior_secs)
                                    if prior_secs
                                    else max(0, int(srt_sec) - gap)
                                )
                                if not submit_prepared_only:
                                    if (
                                        await _chat_has_pending_image_generation(
                                            page
                                        )
                                        or (
                                            watch_prior
                                            and await _srt_success_without_image(
                                                page, int(watch_prior)
                                            )
                                        )
                                        or await _is_generating(page)
                                        or await _page_background_processing(page)
                                    ):
                                        _tab_log(
                                            f"다음명령 대기 SRT_{srt_sec:03d} "
                                            "— 이전 생성·성공문구만 대기"
                                        )
                                    idle_ok = await _wait_until_generation_idle(
                                        page,
                                        timeout_sec=180.0,
                                        stable_hits=3,
                                        label=f"다음씬 SRT_{srt_sec:03d}",
                                        watch_srt_sec=(
                                            int(watch_prior)
                                            if watch_prior
                                            else None
                                        ),
                                    )
                                    if not idle_ok:
                                        _tab_log(
                                            "다음명령 유휴 대기 시간 초과 "
                                            "— 입력 보류 후 재시도"
                                        )
                                        idle_ok = (
                                            await _wait_until_generation_idle(
                                                page,
                                                timeout_sec=120.0,
                                                stable_hits=3,
                                                label=(
                                                    f"다음씬재 SRT_"
                                                    f"{srt_sec:03d}"
                                                ),
                                                watch_srt_sec=(
                                                    int(watch_prior)
                                                    if watch_prior
                                                    else None
                                                ),
                                            )
                                        )
                                    if (
                                        watch_prior
                                        and await _srt_success_without_image(
                                            page, int(watch_prior)
                                        )
                                    ):
                                        raise RuntimeError(
                                            f"SRT_{int(watch_prior):03d} "
                                            "성공 문구만 있고 이미지 없음 "
                                            "— 다음 명령 보류"
                                        )
                                    t_phase = _timing_log(
                                        f"다음명령유휴 SRT_{srt_sec:03d}",
                                        t_phase,
                                        extra=f"idle={idle_ok}",
                                    )
                                    if not idle_ok:
                                        raise RuntimeError(
                                            "이전 이미지 생성이 아직 끝나지 "
                                            "않았습니다 — 첨부·입력 보류"
                                        )
                                page, _ = await _ensure_composer_page(
                                    page,
                                    url,
                                    work_url=work_url or "",
                                    preserve_input=submit_prepared_only,
                                )
                                on_landing_composer = page_sc < 40
                                if attach_reference:
                                    expect_sec, attach_ref_path = (
                                        resolve_strict_reference_png(
                                            png_dir,
                                            srt_sec,
                                            interval_sec=interval_sec,
                                            last_completed_sec=_LAST_SCENE_REF.get(
                                                "sec"
                                            ),
                                            last_completed_path=_LAST_SCENE_REF.get(
                                                "path"
                                            ),
                                            scene_secs=scene_secs_list,
                                        )
                                    )
                                    if attach_ref_path is None:
                                        if expect_sec is None:
                                            _tab_log(
                                                f"참조첨부 생략 "
                                                f"SRT_{srt_sec:03d} — "
                                                "직전 슬롯 없음"
                                            )
                                        else:
                                            _tab_log(
                                                f"참조첨부 생략 "
                                                f"SRT_{srt_sec:03d} — "
                                                f"{srt_png_name(expect_sec)} 없음"
                                            )
                                    else:
                                        _tab_log(
                                            f"참조 예정 SRT_{srt_sec:03d} · "
                                            f"직전슬롯={expect_sec} · "
                                            f"file={attach_ref_path.name} · "
                                            f"명령 끝 Ctrl+V · "
                                            f"{'랜딩' if on_landing_composer else '대화'}"
                                        )
                                if isinstance(
                                    char_state_tracker, CharacterStateTracker
                                ):
                                    _tab_log(
                                        f"상태키 SRT_{srt_sec:03d} · "
                                        f"{char_state_tracker.summary()}"
                                    )
                                forbid = set(seen_file_urls.keys())
                                prev_src = (last_saved_file_url or "").strip()
                                if not prev_src:
                                    prev_src = (
                                        await _genspark_file_src(page) or ""
                                    ).strip()
                                baseline_near = ""
                                send_prompt = ""
                                if submit_prepared_only:
                                    page, _ = await _ensure_composer_page(
                                        page,
                                        url,
                                        work_url=work_url or "",
                                        preserve_input=True,
                                    )
                                    await _install_same_tab_guards(page)
                                    await _raise_if_page_limited(
                                        page,
                                        label=f"입력창준비 SRT_{srt_sec:03d}",
                                    )
                                    prep_want_ref = bool(
                                        attach_reference
                                        and attach_ref_path is not None
                                    )
                                    if prep_want_ref and attach_ref_path is not None:
                                        _tab_log(
                                            f"입력창(준비) 참조 첨부 · "
                                            f"{attach_ref_path.name} · "
                                            f"{_page_ctx_label(page.url or '')}"
                                        )
                                        ref_attached, page = (
                                            await _attach_reference_after_command(
                                                page,
                                                context,
                                                attach_ref_path,
                                                url=url,
                                                work_url=work_url or "",
                                                srt_sec=srt_sec,
                                                watch_prior=int(watch_prior),
                                                ref_follow=False,
                                                interval_sec=interval_sec,
                                                submit_after=True,
                                                png_dir=png_dir,
                                                scene_secs=scene_secs_list,
                                            )
                                        )
                                        if not ref_attached:
                                            raise RuntimeError(
                                                "참조 첨부·전송 실패 — "
                                                "입력창(준비) 전송 보류"
                                            )
                                        ref_path = attach_ref_path
                                    else:
                                        _tab_log(
                                            f"입력창전송(준비) · "
                                            f"{_page_ctx_label(page.url or '')} · "
                                            f"ref=False"
                                        )
                                        await _ensure_submitted(page)
                                    t_phase = _timing_log(
                                        f"입력·전송 SRT_{srt_sec:03d}",
                                        t_phase,
                                        extra=f"prepared ref={ref_attached}",
                                    )
                                else:
                                    if not ref_attached:
                                        page, _ = await _ensure_composer_page(
                                            page, url, work_url=work_url or ""
                                        )
                                        if on_landing_composer:
                                            for _wait_i in range(12):
                                                await page.wait_for_timeout(500)
                                                page = await _alive_page(
                                                    context, page
                                                )
                                                if await _prompt_editor_candidates(
                                                    page,
                                                    prefer_followup=False,
                                                ):
                                                    break
                                                if (
                                                    _score_ai_image_url(
                                                        page.url or ""
                                                    )
                                                    >= 40
                                                ):
                                                    break
                                    if not await _wait_until_generation_idle(
                                        page,
                                        timeout_sec=180.0,
                                        stable_hits=3,
                                        label=f"입력전 SRT_{srt_sec:03d}",
                                    ):
                                        raise RuntimeError(
                                            "이미지 생성 중 — 명령 입력 보류"
                                        )
                                    await _raise_if_page_limited(
                                        page,
                                        label=f"입력전 SRT_{srt_sec:03d}",
                                    )
                                    if await _chat_has_pending_image_generation(
                                        page
                                    ):
                                        raise RuntimeError(
                                            "「이미지 생성」 카드가 아직 "
                                            "끝나지 않았습니다 — 입력 보류"
                                        )
                                    if (
                                        watch_prior
                                        and await _srt_success_without_image(
                                            page, int(watch_prior)
                                        )
                                    ):
                                        raise RuntimeError(
                                            f"SRT_{int(watch_prior):03d} "
                                            "성공 문구만 있고 이미지 없음 "
                                            "— 입력 보류"
                                        )
                                    await _scroll_to_composer(page)
                                    pre_cands = await _prompt_editor_candidates(
                                        page,
                                        prefer_followup=not on_landing_composer,
                                    )
                                    if pre_cands:
                                        stale = (
                                            await _ensure_composer_attachments_clear(
                                                page,
                                                pre_cands[0],
                                                label=(
                                                    f"입력전 SRT_{srt_sec:03d}"
                                                ),
                                            )
                                        )
                                        if stale > 0:
                                            raise RuntimeError(
                                                f"composer 잔여 첨부 "
                                                f"{stale}장 — 입력 보류"
                                            )
                                    want_ref = bool(attach_reference)
                                    slot_sec: int | None = None
                                    if want_ref:
                                        attach_ref_path = None
                                        slot_sec = previous_reference_slot_sec(
                                            srt_sec,
                                            interval_sec=interval_sec,
                                            scene_secs=scene_secs_list,
                                        )
                                        if slot_sec is None:
                                            attach_ref_path = None
                                            _tab_log(
                                                f"참조첨부 생략 "
                                                f"SRT_{srt_sec:03d} — "
                                                "직전 슬롯 없음"
                                            )
                                        else:
                                            for ref_try in range(10):
                                                if pending_fail_secs:
                                                    late = (
                                                        await _salvage_late_images(
                                                            page,
                                                            png_dir,
                                                            pending_fail_secs,
                                                            seen_file_urls,
                                                        )
                                                    )
                                                    if late:
                                                        last_saved_file_url = (
                                                            late
                                                        )
                                                _slot, attach_ref_path = (
                                                    resolve_strict_reference_png(
                                                        png_dir,
                                                        srt_sec,
                                                        interval_sec=interval_sec,
                                                        last_completed_sec=_LAST_SCENE_REF.get(
                                                            "sec"
                                                        ),
                                                        last_completed_path=_LAST_SCENE_REF.get(
                                                            "path"
                                                        ),
                                                        scene_secs=scene_secs_list,
                                                    )
                                                )
                                                if _slot is not None:
                                                    slot_sec = _slot
                                                if attach_ref_path is not None:
                                                    break
                                                if ref_try == 0:
                                                    _tab_log(
                                                        f"참조 대기 "
                                                        f"SRT_{srt_sec:03d} · "
                                                        f"{srt_png_name(slot_sec)}"
                                                    )
                                                await page.wait_for_timeout(
                                                    2500
                                                )
                                            if attach_ref_path is None:
                                                raise RuntimeError(
                                                    f"직전 슬롯 "
                                                    f"{srt_png_name(slot_sec)} "
                                                    f"없음 — "
                                                    f"SRT_{srt_sec:03d} "
                                                    "명령 입력·전송 보류"
                                                )
                                            else:
                                                _tab_log(
                                                    f"참조 확정 "
                                                    f"SRT_{srt_sec:03d} · "
                                                    f"직전슬롯={slot_sec} · "
                                                    f"file="
                                                    f"{attach_ref_path.name}"
                                                )
                                    ref_will_attach = (
                                        want_ref
                                        and attach_ref_path is not None
                                    )
                                    if ref_will_attach:
                                        ref_label = attach_ref_path.stem
                                    send_prompt = (
                                        build_generate_command_from_sources(
                                            srt_sec,
                                            scene_prompt=prompt_raw or None,
                                            srt_path=srt_path,
                                            interval_sec=interval_sec,
                                            png_dir=png_dir,
                                            state_tracker=char_state_tracker,
                                            prompt_path=prompt_path,
                                            reference_attached=ref_will_attach,
                                            reference_label=ref_label,
                                            next_scene_sec=next_scene_sec,
                                        )
                                    )
                                    t_phase = _timing_log(
                                        f"준비 SRT_{srt_sec:03d}",
                                        t_phase,
                                        extra=(
                                            f"chars={len(send_prompt)} · "
                                            f"ref={ref_will_attach}"
                                        ),
                                    )
                                    if not await _fill_first_editable(
                                        page,
                                        send_prompt,
                                        prefer_followup=not on_landing_composer,
                                        skip_ready_wait=True,
                                        after_image_attach=False,
                                    ):
                                        raise RuntimeError(
                                            "이어쓰기 입력란을 찾지 못했습니다."
                                        )
                                    if ref_will_attach:
                                        ref_follow = (
                                            page_sc >= 40
                                            or "/agents"
                                            in (page.url or "").lower()
                                        )
                                        ref_attached, page = (
                                            await _attach_reference_after_command(
                                                page,
                                                context,
                                                attach_ref_path,
                                                url=url,
                                                work_url=work_url or "",
                                                srt_sec=srt_sec,
                                                watch_prior=int(watch_prior),
                                                ref_follow=ref_follow,
                                                interval_sec=interval_sec,
                                                png_dir=png_dir,
                                                scene_secs=scene_secs_list,
                                            )
                                        )
                                        if not ref_attached:
                                            raise RuntimeError(
                                                "참조 첨부·전송 실패 — "
                                                "명령 입력·전송 보류"
                                            )
                                        ref_path = attach_ref_path
                                    elif not ref_will_attach:
                                        await _ensure_submitted(page)
                                    t_phase = _timing_log(
                                        f"입력·전송 SRT_{srt_sec:03d}",
                                        t_phase,
                                        extra=f"chars={len(send_prompt)}",
                                    )
                                _resubmit_ref_path = attach_ref_path
                                if (
                                    attach_reference
                                    and (
                                        _resubmit_ref_path is None
                                        or not _resubmit_ref_path.is_file()
                                    )
                                ):
                                    _, _resubmit_ref_path = (
                                        resolve_strict_reference_png(
                                            png_dir,
                                            srt_sec,
                                            interval_sec=interval_sec,
                                            last_completed_sec=_LAST_SCENE_REF.get(
                                                "sec"
                                            ),
                                            last_completed_path=_LAST_SCENE_REF.get(
                                                "path"
                                            ),
                                            scene_secs=scene_secs_list,
                                        )
                                    )
                                _resubmit_ref_attach = bool(
                                    attach_reference
                                    and _resubmit_ref_path is not None
                                    and _resubmit_ref_path.is_file()
                                )
                                if not send_prompt.strip():
                                    _ref_l = (
                                        _resubmit_ref_path.stem
                                        if _resubmit_ref_attach
                                        and _resubmit_ref_path is not None
                                        else ""
                                    )
                                    send_prompt = (
                                        build_generate_command_from_sources(
                                            srt_sec,
                                            scene_prompt=prompt_raw or None,
                                            srt_path=srt_path,
                                            interval_sec=interval_sec,
                                            png_dir=png_dir,
                                            state_tracker=char_state_tracker,
                                            prompt_path=prompt_path,
                                            reference_attached=_resubmit_ref_attach,
                                            reference_label=_ref_l,
                                            next_scene_sec=next_scene_sec,
                                        )
                                    )
                                dest = png_dir / srt_png_name(srt_sec)
                                saved: Path | None = None
                                file_src = ""
                                scene_saved = False
                                for resubmit_i in range(
                                    PHANTOM_RESUBMIT_MAX + 1
                                ):
                                    if resubmit_i > 0:
                                        prep_ok, page, ref_attached = (
                                            await _resubmit_scene_after_phantom(
                                                page,
                                                context,
                                                srt_sec=srt_sec,
                                                send_prompt=send_prompt,
                                                attach_ref_path=(
                                                    _resubmit_ref_path
                                                    if _resubmit_ref_attach
                                                    else None
                                                ),
                                                url=url,
                                                work_url=work_url or "",
                                                watch_prior=int(watch_prior),
                                                interval_sec=interval_sec,
                                                on_landing_composer=(
                                                    on_landing_composer
                                                ),
                                                resubmit_i=resubmit_i,
                                                png_dir=png_dir,
                                                scene_secs=scene_secs_list,
                                            )
                                        )
                                        if not prep_ok:
                                            break
                                        if ref_attached and _resubmit_ref_path:
                                            ref_path = _resubmit_ref_path
                                    else:
                                        _tab_log(
                                            f"제출 후 last_saved="
                                            f"{normalize_genspark_file_url(prev_src)[:80]}"
                                        )
                                        try:
                                            await page.wait_for_timeout(200)
                                        except Exception:
                                            page = await _alive_page(
                                                context, page
                                            )
                                        page = await _alive_page(
                                            context, page
                                        )
                                        if page.url and "genspark" in (
                                            page.url or ""
                                        ).lower():
                                            work_url = _maybe_set_work_url(
                                                work_url, page.url
                                            )
                                    t_gen = time.perf_counter()
                                    ok, page = await _wait_generation_done(
                                        page,
                                        baseline_count=0,
                                        prev_src=prev_src,
                                        timeout_sec=max(120, gen_timeout),
                                        context=context,
                                        baseline_failures=fail_before,
                                        srt_sec=srt_sec,
                                        baseline_near_src=baseline_near,
                                        forbid_keys=forbid,
                                        last_saved_src=prev_src,
                                    )
                                    t_phase = _timing_log(
                                        f"생성대기구간 SRT_{srt_sec:03d}",
                                        t_gen,
                                        extra=(
                                            f"ok={ok} · "
                                            f"retry={resubmit_i}"
                                        ),
                                    )
                                    if await _page_shows_failure(
                                        page, baseline_failures=fail_before
                                    ) or (
                                        await _srt_label_shows_failure(
                                            page, srt_sec
                                        )
                                        and not await _srt_success_message_seen(
                                            page, srt_sec
                                        )
                                    ):
                                        raise RuntimeError(
                                            "Failure 메시지 감지 — "
                                            "다음 씬으로 진행"
                                        )
                                    if not ok:
                                        hit = await detect_limit_on_page(page)
                                        if hit is not None:
                                            raise_limit_error(hit)
                                        phantom = (
                                            await _srt_success_without_image(
                                                page, srt_sec
                                            )
                                            or (
                                                await _srt_success_message_seen(
                                                    page, srt_sec
                                                )
                                                and not (
                                                    await _file_src_near_srt_label(
                                                        page,
                                                        srt_sec,
                                                        forbid_keys=forbid,
                                                    )
                                                ).strip()
                                            )
                                        )
                                        if (
                                            phantom
                                            and resubmit_i
                                            < PHANTOM_RESUBMIT_MAX
                                        ):
                                            continue
                                        if await _srt_success_message_seen(
                                            page, srt_sec
                                        ):
                                            raise RuntimeError(
                                                "이미지는 없고 성공 문구만 있음 "
                                                "— 다음 씬으로 진행"
                                            )
                                        raise RuntimeError(
                                            f"새 이미지 대기 초과 "
                                            f"({max(120, gen_timeout)}s) "
                                            "— 다음 씬으로 진행"
                                        )
                                    t_dl = time.perf_counter()
                                    try:
                                        saved, file_src = (
                                            await _save_latest_image_to(
                                                page,
                                                dest,
                                                prefer_button=False,
                                                forbid_keys=forbid,
                                                require_new_vs=prev_src,
                                                srt_sec=srt_sec,
                                            )
                                        )
                                    except RuntimeError as dl_ex:
                                        _tab_log(
                                            f"다운로드 실패 SRT_{srt_sec:03d}: "
                                            f"{dl_ex}"
                                        )
                                        phantom_dl = (
                                            await _srt_success_without_image(
                                                page, srt_sec
                                            )
                                            or "새 이미지" in str(dl_ex)
                                            or "요청 아래" in str(dl_ex)
                                        )
                                        if (
                                            phantom_dl
                                            and resubmit_i
                                            < PHANTOM_RESUBMIT_MAX
                                        ):
                                            continue
                                        raise
                                    t_phase = _timing_log(
                                        f"다운로드구간 SRT_{srt_sec:03d}",
                                        t_dl,
                                        extra=dest.name,
                                    )
                                    if (
                                        not saved.is_file()
                                        or saved.stat().st_size < 512
                                    ):
                                        if (
                                            resubmit_i
                                            < PHANTOM_RESUBMIT_MAX
                                        ):
                                            continue
                                        raise RuntimeError(
                                            f"다운로드 후 파일 없음: "
                                            f"{dest.name}"
                                        )
                                    scene_saved = True
                                    break
                                if not scene_saved:
                                    hit = await detect_limit_on_page(page)
                                    if hit is not None:
                                        raise_limit_error(hit)
                                    if await _srt_success_message_seen(
                                        page, srt_sec
                                    ):
                                        raise RuntimeError(
                                            "이미지는 없고 성공 문구만 있음 "
                                            "(재전송 후) — 다음 씬으로 진행"
                                        )
                                    raise RuntimeError(
                                        f"새 이미지 대기·다운로드 실패 "
                                        f"SRT_{srt_sec:03d}"
                                    )
                                key = _image_url_key(file_src)
                                if key:
                                    seen_file_urls[key] = dest.name
                                    _save_seen_file_urls(png_dir, seen_file_urls)
                                    last_saved_file_url = file_src
                                saved_paths = [str(saved)]
                                page = await _alive_page(context, page)
                                t_idle = time.perf_counter()
                                idle_ok = await _wait_until_generation_idle(
                                    page,
                                    timeout_sec=30.0,
                                    stable_hits=2,
                                    label=f"다운로드후 SRT_{srt_sec:03d}",
                                    watch_srt_sec=srt_sec,
                                )
                                if not idle_ok:
                                    idle_ok = await _wait_idle_after_download(
                                        page,
                                        timeout_sec=20.0,
                                        stable_hits=2,
                                    )
                                _LAST_SCENE_IDLE["at"] = time.time()
                                _LAST_SCENE_IDLE["ok"] = bool(idle_ok)
                                _LAST_SCENE_REF["sec"] = int(srt_sec)
                                _LAST_SCENE_REF["path"] = str(saved)
                                _LAST_SCENE_REF["file_url"] = file_src or ""
                                _timing_log(
                                    f"다운로드후유휴 SRT_{srt_sec:03d}",
                                    t_idle,
                                    extra=f"idle={idle_ok}",
                                )
                                _timing_log(
                                    f"씬합계 SRT_{srt_sec:03d}",
                                    t_scene,
                                    extra=f"{key[:70]}",
                                )
                                if page.url:
                                    work_url = _maybe_set_work_url(work_url, page.url)
                                resp_q.put(
                                    (
                                        True,
                                        {
                                            "ok": True,
                                            "attempt": 1,
                                            "regenerated": False,
                                            "saved": saved_paths,
                                            "file_url": file_src,
                                            "reference_attached": ref_attached,
                                            "reference_file": (
                                                str(ref_path)
                                                if ref_attached and ref_path
                                                else ""
                                            ),
                                        },
                                        None,
                                    )
                                )
                                break
                            except Exception as ex:
                                last_err = str(ex)
                                if srt_sec not in pending_fail_secs:
                                    pending_fail_secs.append(int(srt_sec))
                                # 페이지 스니펫을 실패 로그에 남김
                                page_snip = ""
                                try:
                                    from scene_image.image_log import append_fail_log

                                    page_snip = await read_page_visible_text(
                                        page, max_chars=2500
                                    )
                                    kind = (
                                        "limit"
                                        if isinstance(ex, AiImageLimitError)
                                        else "fail"
                                    )
                                    extra = ""
                                    if isinstance(ex, AiImageLimitError) and ex.reset_at:
                                        extra = (
                                            "reset_at="
                                            + ex.reset_at.strftime("%Y-%m-%d %H:%M")
                                        )
                                    d = _TAB_LOG_PNG.get("dir")
                                    if d is not None:
                                        append_fail_log(
                                            d,
                                            scene=f"SRT_{int(srt_sec):03d}",
                                            error=last_err,
                                            kind=kind,
                                            page_snip=page_snip or getattr(
                                                ex, "raw", ""
                                            ),
                                            extra=extra,
                                        )
                                except Exception:
                                    pass
                                _tab_log(
                                    f"run_scene 실패(재시도 없음): {last_err}"
                                )
                                # AiImageLimitError·BrowserClosedError 타입 유지
                                err_out: BaseException
                                if isinstance(ex, AiImageLimitError):
                                    err_out = ex
                                elif isinstance(ex, BrowserClosedError):
                                    err_out = ex
                                elif is_browser_closed_error(ex):
                                    err_out = BrowserClosedError(last_err)
                                else:
                                    err_out = RuntimeError(last_err)
                                resp_q.put(
                                    (
                                        False,
                                        None,
                                        err_out,
                                    )
                                )
                                break
                            finally:
                                merge_pause["v"] = False
                        else:
                            if not saved_paths:
                                resp_q.put(
                                    (
                                        False,
                                        None,
                                        RuntimeError(last_err or "씬 생성 실패"),
                                    )
                                )
                    elif op == "collect":
                        wait_ms = int(arg or 2000)
                        await page.wait_for_timeout(max(0, wait_ms))
                        items = await _collect_images(page)
                        resp_q.put((True, items, None))
                    elif op == "download":
                        data = arg or {}
                        png_dir = Path(data.get("png_dir") or ".")
                        png_dir.mkdir(parents=True, exist_ok=True)
                        from scene_image.download import assign_srt_secs, download_url

                        resolved = assign_srt_secs(
                            list(data.get("items") or []),
                            fallback_secs=data.get("fallback_secs"),
                            default_start_sec=data.get("default_start_sec"),
                        )
                        saved: list[str] = []
                        for n, url in resolved:
                            dest = png_dir / srt_png_name(n)
                            if url.startswith("blob:"):
                                b64 = await page.evaluate(
                                    """async (u) => {
                                      const r = await fetch(u);
                                      const buf = await r.arrayBuffer();
                                      const bytes = new Uint8Array(buf);
                                      let s = '';
                                      for (let i = 0; i < bytes.length; i++)
                                        s += String.fromCharCode(bytes[i]);
                                      return btoa(s);
                                    }""",
                                    url,
                                )
                                dest.write_bytes(base64.b64decode(b64))
                            else:
                                download_url(url, dest)
                            saved.append(str(dest))
                        resp_q.put((True, saved, None))
                    elif op == "salvage":
                        data = arg or {}
                        png_dir = Path(data.get("png_dir") or ".")
                        png_dir.mkdir(parents=True, exist_ok=True)
                        seen_file_urls.update(_load_seen_file_urls(png_dir))
                        requested: list[int] = []
                        for s in data.get("secs") or []:
                            try:
                                sec_i = int(s)
                            except (TypeError, ValueError):
                                continue
                            requested.append(sec_i)
                            if sec_i not in pending_fail_secs:
                                pending_fail_secs.append(sec_i)
                        check_secs = sorted(set(requested) | set(pending_fail_secs))
                        missing_before = [
                            s for s in check_secs if not png_already_exists(png_dir, s)
                        ]
                        if not _context_has_live_page(context):
                            _tab_log("salvage 생략 — 브라우저 종료")
                            resp_q.put(
                                (
                                    True,
                                    {
                                        "recovered": 0,
                                        "still_missing": missing_before,
                                        "saved": [],
                                        "recovered_secs": [],
                                        "browser_closed": True,
                                    },
                                    None,
                                )
                            )
                            continue
                        try:
                            page = await _alive_page(context, page)
                            if pending_fail_secs:
                                late = await _salvage_late_images(
                                    page,
                                    png_dir,
                                    pending_fail_secs,
                                    seen_file_urls,
                                )
                                if late:
                                    last_saved_file_url = late
                        except BrowserClosedError:
                            _tab_log("salvage 생략 — 브라우저 종료")
                            resp_q.put(
                                (
                                    True,
                                    {
                                        "recovered": 0,
                                        "still_missing": missing_before,
                                        "saved": [],
                                        "recovered_secs": [],
                                        "browser_closed": True,
                                    },
                                    None,
                                )
                            )
                            continue
                        still_missing = [
                            s for s in check_secs if not png_already_exists(png_dir, s)
                        ]
                        recovered_secs = [
                            s for s in missing_before if s not in still_missing
                        ]
                        saved_paths = [
                            str((png_dir / srt_png_name(s)).resolve())
                            for s in recovered_secs
                        ]
                        _tab_log(
                            f"salvage 완료 · 회수 {len(recovered_secs)} · "
                            f"미수신 {len(still_missing)}"
                            + (
                                f" · {still_missing[:12]}"
                                if still_missing
                                else ""
                            )
                        )
                        resp_q.put(
                            (
                                True,
                                {
                                    "recovered": len(recovered_secs),
                                    "still_missing": still_missing,
                                    "saved": saved_paths,
                                    "recovered_secs": recovered_secs,
                                },
                                None,
                            )
                        )
                    elif op == "stop":
                        resp_q.put((True, None, None))
                        break
                    elif op == "probe_limit":
                        data = arg or {}
                        url = data.get("url") or GENSPARK_AI_IMAGE_URL
                        email = str(data.get("email") or "")
                        password = str(data.get("password") or "")
                        page = await _alive_page(context, page)
                        urls_to_try: list[str] = []
                        for candidate in (
                            (work_url or "").strip(),
                            (page.url or "").strip(),
                            str(url or "").strip(),
                        ):
                            if (
                                candidate
                                and "genspark.ai" in candidate.lower()
                                and candidate not in urls_to_try
                            ):
                                urls_to_try.append(candidate)
                        if not urls_to_try:
                            urls_to_try.append(str(url or GENSPARK_AI_IMAGE_URL))

                        async def _ensure_logged_in_on_page() -> None:
                            if email and password and not await _is_logged_in(page):
                                await _ensure_login(
                                    page,
                                    email,
                                    password,
                                    context=context,
                                    force=False,
                                )

                        hit = None
                        snippet = ""
                        for try_url in urls_to_try:
                            cur = (page.url or "").strip()
                            if try_url not in cur:
                                await page.goto(
                                    try_url,
                                    wait_until="domcontentloaded",
                                    timeout=90_000,
                                )
                                await page.wait_for_timeout(1500)
                            await _ensure_logged_in_on_page()
                            if try_url not in (page.url or ""):
                                await page.goto(
                                    try_url,
                                    wait_until="domcontentloaded",
                                    timeout=90_000,
                                )
                                await page.wait_for_timeout(1500)
                            await page.wait_for_timeout(2500)
                            hit = await detect_limit_on_page(page)
                            if hit is not None:
                                snippet = hit.snippet
                                break
                            page_text = await read_page_visible_text(page)
                            hit = limit_hit_from_text(page_text)
                            if hit is not None:
                                snippet = hit.snippet or page_text[:800]
                                break
                            snippet = page_text[:800]
                        reset_at = hit.reset_at if hit else None
                        if reset_at is None and snippet:
                            reset_at = parse_reset_at(snippet)
                        resp_q.put(
                            (
                                True,
                                {
                                    "reset_at": (
                                        reset_at.strftime("%Y-%m-%dT%H:%M:%S")
                                        if reset_at
                                        else None
                                    ),
                                    "snippet": snippet[:800],
                                    "is_limit": hit is not None,
                                    "message": hit.message if hit else "",
                                },
                                None,
                            )
                        )
                    else:
                        resp_q.put(
                            (False, None, RuntimeError(f"알 수 없는 명령: {op}"))
                        )
                except Exception as ex:
                    resp_q.put((False, None, ex))


_session: GensparkSceneSession | None = None
_session_lock = threading.Lock()


def reset_image_session() -> None:
    global _session
    with _session_lock:
        if _session is not None:
            try:
                _session._call("stop", timeout=10.0)
            except Exception:
                pass
            _session = None


def get_image_session(profile_dir: Path) -> GensparkSceneSession:
    global _session
    with _session_lock:
        if _session is None:
            _session = GensparkSceneSession(profile_dir)
        return _session

# -*- coding: utf-8 -*-
"""2_5_sceneImage ChromeDebug 슬롯 — 인스턴스별 포트·프로필 자동 할당."""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path

# 모듈 기본 포트. 슬롯 N → base+N / user_data_slot{N}
_CDP_BASE_PORT = 9242
_MAX_SLOTS = 8
_LOCK_DIR = Path(r"C:\ChromeDebug_2_5\.slots")
_LEGACY_USER_DATA = Path(r"C:\ChromeDebug_2_5")
_USER_DATA_SLOT_PREFIX = Path(r"C:\ChromeDebug_2_5_slot")


def configure_chrome_slot_module(
    *,
    base_port: int = 9242,
    lock_dir: Path | str | None = None,
    legacy_user_data: Path | str | None = None,
    user_data_slot_prefix: Path | str | None = None,
) -> None:
    """ChromeDebug 슬롯 범위·프로필 경로 (sceneImage / sceneImageScript 등)."""
    global _CDP_BASE_PORT, _LOCK_DIR, _LEGACY_USER_DATA, _USER_DATA_SLOT_PREFIX
    _CDP_BASE_PORT = int(base_port)
    if lock_dir is not None:
        _LOCK_DIR = Path(lock_dir)
    if legacy_user_data is not None:
        _LEGACY_USER_DATA = Path(legacy_user_data)
    if user_data_slot_prefix is not None:
        _USER_DATA_SLOT_PREFIX = Path(user_data_slot_prefix)

_lock = threading.Lock()
_active: ChromeSlot | None = None


@dataclass(frozen=True)
class ChromeSlot:
    index: int
    port: int
    user_data: Path

    @property
    def label(self) -> str:
        return f"slot{self.index}:{self.port}"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid)
            )
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False
        except Exception:
            return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def user_data_for_slot(index: int) -> Path:
    """슬롯별 user-data-dir. slot0 은 레거시 프로필을 우선."""
    modern = Path(f"{_USER_DATA_SLOT_PREFIX}{int(index)}")
    if int(index) == 0:
        legacy = _LEGACY_USER_DATA
        if (legacy / "Default").is_dir() and not (modern / "Default").is_dir():
            return legacy
    return modern


def port_for_slot(index: int) -> int:
    return int(_CDP_BASE_PORT) + int(index)


def get_active_slot() -> ChromeSlot | None:
    with _lock:
        return _active


def count_claimable_slots() -> int:
    """다른 프로세스가 점유하지 않은 슬롯 수 (신규 인스턴스용)."""
    _LOCK_DIR.mkdir(parents=True, exist_ok=True)
    me = os.getpid()
    free = 0
    for i in range(_MAX_SLOTS):
        lock_path = _LOCK_DIR / f"slot_{i}.pid"
        if not lock_path.is_file():
            free += 1
            continue
        try:
            old = int(lock_path.read_text(encoding="utf-8").strip().splitlines()[0])
        except (OSError, ValueError, IndexError):
            free += 1
            continue
        if not _pid_alive(old):
            free += 1
        elif old != me:
            pass  # 다른 프로세스 점유
        # old == me: 이 창이 사용 중 — 신규 인스턴스용으로 세지 않음
    return free


def ensure_chrome_slot() -> ChromeSlot:
    """이 프로세스용 슬롯을 확보(이미 있으면 재사용)."""
    global _active
    with _lock:
        if _active is not None:
            return _active
        _LOCK_DIR.mkdir(parents=True, exist_ok=True)
        me = os.getpid()
        last_err: Exception | None = None
        for i in range(_MAX_SLOTS):
            lock_path = _LOCK_DIR / f"slot_{i}.pid"
            try:
                if not _try_claim_lock(lock_path, me):
                    continue
            except OSError as e:
                last_err = e
                continue
            slot = ChromeSlot(
                index=i,
                port=port_for_slot(i),
                user_data=user_data_for_slot(i),
            )
            _active = slot
            return slot
        msg = f"ChromeDebug 슬롯이 모두 사용 중입니다 (최대 {_MAX_SLOTS}개)."
        if last_err is not None:
            msg = f"{msg}\n{last_err}"
        raise RuntimeError(msg)


def _try_claim_lock(lock_path: Path, pid: int) -> bool:
    """락 파일이 없거나 stale 이면 이 PID로 점유. 성공 시 True."""
    for _ in range(3):
        if lock_path.is_file():
            try:
                old = int(lock_path.read_text(encoding="utf-8").strip().splitlines()[0])
            except (OSError, ValueError, IndexError):
                old = -1
            if _pid_alive(old) and old != pid:
                return False
            try:
                lock_path.unlink()
            except OSError:
                return False
        try:
            fd = os.open(
                str(lock_path),
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )
        except FileExistsError:
            continue
        try:
            os.write(fd, f"{pid}\n".encode("ascii"))
        finally:
            os.close(fd)
        return True
    return False


def release_chrome_slot() -> None:
    """프로세스 종료 시 슬롯 락 해제. Chrome 프로세스는 그대로 둔다."""
    global _active
    with _lock:
        slot = _active
        _active = None
    if slot is None:
        return
    lock_path = _LOCK_DIR / f"slot_{slot.index}.pid"
    try:
        if lock_path.is_file():
            raw = lock_path.read_text(encoding="utf-8").strip().splitlines()[0]
            if int(raw) == os.getpid():
                lock_path.unlink()
    except (OSError, ValueError, IndexError):
        pass

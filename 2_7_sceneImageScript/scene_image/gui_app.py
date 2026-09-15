# -*- coding: utf-8 -*-
"""2_5_sceneImage GUI — 루트/stt·mp3·png + 모듈 md → Genspark 생성."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, font as tkfont, ttk

from scene_image import __version__
from scene_image.character_bible import clear_registry_cache, get_registry
from scene_image.credentials import load_credentials, save_credentials
from scene_image.scene_parse import find_previous_reference_png
from scene_image.genspark_image import (
    build_generate_command_from_sources,
    clear_chrome_session_restore,
    close_chrome_debug,
    get_image_session,
    has_playwright,
    image_profile_dir,
    open_browser_for_account,
    preferred_genspark_url,
    reset_image_session,
    set_tab_log_png_dir,
)
from scene_image.chrome_slot import (
    configure_chrome_slot_module,
    count_claimable_slots,
    ensure_chrome_slot,
    get_active_slot,
    release_chrome_slot,
)
from scene_image.image_log import append_fail_log, append_image_log
from scene_image.limit_detect import (
    AiImageLimitError,
    BrowserClosedError,
    format_reset_at,
    is_browser_closed_error,
    parse_reset_at,
    parse_session_start_hm,
    resolve_limit_reset_at,
    text_is_near_limit_only,
    text_looks_like_limit,
)
from scene_image.paths import (
    GENSPARK_AI_IMAGE_URL,
    build_paste_payload,
    default_png_dir,
    default_root_dir,
    ensure_root_layout,
    find_default_srt,
    find_image_prompt_file,
    find_prompt_in_md,
    load_scene_text,
    module_md_dir,
    paste_payload_stats,
    png_dir_under_root,
)
from scene_image.pipeline_config import load_pipeline_config, model_name_variants
from scene_image.scene_parse import (
    SceneLine,
    build_interval_scenes,
    is_real_scene_prompt,
    parse_scene_script,
    parse_sec_selection,
    png_already_exists,
    previous_reference_slot_sec,
    scene_png_path,
    srt_dialogue_for_window,
    srt_dialogue_until_next_scene,
    srt_png_name,
)
from scene_image.settings import (
    load_gui_settings,
    load_model_selector,
    save_gui_settings,
    set_config_app,
    set_config_dist,
    set_config_slot,
)
from wisdom_workspace import folder_dialog_initial, touch_workspace_from_path

_SCENE_INTERVAL_SEC = 20
# 한도: 배너 감지 → 재설정 시각까지 대기 (매시간 시험 없음)
_LIMIT_FAIL_STREAK = 2
_LIMIT_WAIT_CHUNK_SEC = 15
_LIMIT_RESET_BUFFER_SEC = 30
_SHUTDOWN_DELAY_SEC = 60  # 완료 후 종료까지 여유(취소: shutdown /a)
_LIMIT_ERR_RE = re.compile(
    r"rate\s*limit|usage\s*limit|fair[\s-]*use|try\s*again\s*later|"
    r"too\s*many|5[\s-]*hour|quota|"
    r"AI\s*Image|"
    r"한도|5\s*시간\s*제한|제한에\s*도달|재설정됩니다|"
    r"사용\s*제한|이용\s*제한|나중에\s*다시|제한에\s*걸",
    re.IGNORECASE,
)


def _schedule_pc_shutdown(*, delay_sec: int) -> None:
    """Windows: shutdown /s /t N — 취소는 shutdown /a."""
    sec = max(1, int(delay_sec))
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.run(
        ["shutdown", "/s", "/t", str(sec)],
        check=False,
        creationflags=flags,
    )


def _load_shutdown_after_complete(cfg: dict[str, str]) -> bool:
    """체크박스 · 구버전(시간 콤보) 설정도 켜짐으로 인식."""
    if (cfg.get("shutdown_after_complete") or "0").strip() in (
        "1",
        "true",
        "True",
        "yes",
        "on",
    ):
        return True
    raw = (cfg.get("shutdown_after_hours") or "").strip()
    if raw.isdigit() and int(raw) > 0:
        return True
    return False


def _reset_at_from_probe(probed: dict | None) -> datetime | None:
    """``probe_limit_reset`` 결과에서 재설정 시각 추출."""
    if not probed:
        return None
    raw_at = (probed or {}).get("reset_at")
    if raw_at:
        try:
            return datetime.strptime(str(raw_at), "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            parsed = resolve_limit_reset_at(str(raw_at))
            if parsed is not None:
                return parsed
    snip = str((probed or {}).get("snippet") or "")
    if snip:
        parsed2 = parse_reset_at(snip)
        if parsed2 is not None:
            return parsed2
    return None


def _looks_like_limit_error(err: str) -> bool:
    if isinstance(err, AiImageLimitError):
        return True
    s = err if isinstance(err, str) else str(err or "")
    # 「5시간 제한에 근접했습니다」는 한도 대기·브라우저 종료 대상 아님
    if text_is_near_limit_only(s):
        return False
    return bool(_LIMIT_ERR_RE.search(s)) or text_looks_like_limit(s)


def _format_scene_time(sec: int) -> str:
    sec = max(0, int(sec))
    m, s = divmod(sec, 60)
    if sec >= 3600:
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _resolve_scene_image_exe(*, script_preview: bool = False) -> Path | None:
    """독립 GUI exe 경로 (허브·소스 실행 포함)."""
    exe_name = (
        "2_7_sceneImageScript_gui.exe"
        if script_preview
        else "2_5_sceneImage_gui.exe"
    )
    module_name = "2_7_sceneImageScript" if script_preview else "2_5_sceneImage"
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).resolve()
        if exe.name.casefold() == exe_name.casefold():
            return exe
    try:
        from wisdom_root import resolve_wisdom_root

        cand = resolve_wisdom_root() / module_name / "dist" / exe_name
        if cand.is_file():
            return cand
    except Exception:
        pass
    cand = Path(__file__).resolve().parents[1] / "dist" / exe_name
    if cand.is_file():
        return cand
    if script_preview:
        alt = (
            Path(__file__).resolve().parents[2]
            / "2_7_sceneImageScript"
            / "dist"
            / exe_name
        )
        return alt if alt.is_file() else None
    return None


def _spawn_scene_image_instance(*, script_preview: bool = False) -> None:
    """다른 Chrome 슬롯으로 sceneImage(또는 Script) 를 병렬 실행."""
    label = "sceneImageScript" if script_preview else "sceneImage"
    exe_name = (
        "2_7_sceneImageScript_gui.exe"
        if script_preview
        else "2_5_sceneImage_gui.exe"
    )
    free = count_claimable_slots()
    if free <= 0:
        raise RuntimeError(
            "ChromeDebug 슬롯이 모두 사용 중입니다 (최대 8개).\n"
            f"다른 {label} 창을 닫은 뒤 다시 시도하세요."
        )
    exe = _resolve_scene_image_exe(script_preview=script_preview)
    if script_preview:
        module_dir = (
            Path(__file__).resolve().parents[2] / "2_7_sceneImageScript"
        )
        if not module_dir.is_dir():
            module_dir = Path(__file__).resolve().parents[1]
    else:
        module_dir = Path(__file__).resolve().parents[1]
    kwargs: dict = {"close_fds": True}
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    if exe is not None:
        kwargs["cwd"] = str(exe.parent)
        subprocess.Popen([str(exe)], **kwargs)
        return
    launcher = (
        module_dir / "run_scene_image_script_gui.py"
        if script_preview
        else module_dir / "run_scene_image_gui.py"
    )
    if not launcher.is_file():
        raise RuntimeError(
            f"{exe_name} 를 찾을 수 없습니다.\n"
            f"build 후 dist 에 exe 가 있어야 합니다.\n({module_dir / 'dist'})"
        )
    env = os.environ.copy()
    env["SCENE_IMAGE_GUI_SOURCE"] = "1"
    kwargs["cwd"] = str(module_dir)
    kwargs["env"] = env
    subprocess.Popen([sys.executable, str(launcher)], **kwargs)


def _default_font() -> tuple[str, int]:
    try:
        f = tkfont.nametofont("TkDefaultFont")
        return (f.actual("family"), max(10, int(f.actual("size"))))
    except tk.TclError:
        return ("맑은 고딕", 10)


def _parse_manual_secs(text: str, available_secs: list[int] | None = None) -> list[int]:
    return parse_sec_selection(text, available_secs)


def _parse_single_sec(text: str) -> int | None:
    """단일 이미지 번호 — ``120`` · ``SRT_120``."""
    raw = (text or "").strip()
    if not raw:
        return None
    m = re.match(r"^SRT[_\s-]?(\d{1,6})\s*$", raw, re.IGNORECASE)
    if m:
        return int(m.group(1))
    if raw.isdigit():
        return int(raw)
    return None


def main(*, container: tk.Misc | None = None, script_preview: bool = False) -> None:
    app_label = "2_7 sceneImageScript" if script_preview else "2_5 sceneImage"
    if script_preview:
        configure_chrome_slot_module(
            base_port=9262,
            lock_dir=Path(r"C:\ChromeDebug_2_7_script\.slots"),
            legacy_user_data=Path(r"C:\ChromeDebug_2_7_script"),
            user_data_slot_prefix=Path(r"C:\ChromeDebug_2_7_script_slot"),
        )
        set_config_app("script")
    from wisdom_gui_host import (
        apply_window_chrome,
        bind_close,
        bind_hub_destroy,
        bind_path_entry_dnd,
        bind_path_row_dnd,
        run_mainloop,
        safe_after,
        safe_messagebox,
        show_toast,
        tk_host,
    )

    root, standalone = tk_host(container)
    hub_flag = (
        "_scene_image_script_gui_built"
        if script_preview
        else "_scene_image_gui_built"
    )
    if not standalone and getattr(root, hub_flag, False):
        return
    if not standalone:
        setattr(root, hub_flag, True)

    try:
        chrome_slot = ensure_chrome_slot()
    except RuntimeError as e:
        if standalone:
            from tkinter import messagebox

            messagebox.showerror(app_label, str(e))
            return
        raise
    set_config_slot(chrome_slot.index)

    apply_window_chrome(
        root,
        standalone,
        title=(
            f"{app_label} {__version__} "
            f"[{chrome_slot.label}]"
        ),
        minsize=(900 if script_preview else 780, 680),
        geometry="1120x820" if script_preview else "960x780",
    )
    fam, sz = _default_font()
    root.option_add("*Font", (fam, sz))

    cfg = load_gui_settings()
    root_default = cfg.get("root_dir") or str(default_root_dir())
    png_default = cfg.get("png_dir") or str(png_dir_under_root(root_default))
    url_default = cfg.get("genspark_url") or GENSPARK_AI_IMAGE_URL
    srt_default = cfg.get("srt_path") or ""
    prompt_default = cfg.get("prompt_path") or ""
    hourly_retry_default = (cfg.get("hourly_limit_retry") or "1").strip() in (
        "1",
        "true",
        "True",
        "yes",
        "on",
    )
    prev_ref_default = (cfg.get("prev_image_reference") or "1").strip() in (
        "1",
        "true",
        "True",
        "yes",
        "on",
    )
    session_start_default = cfg.get("limit_session_start") or ""
    shutdown_default = _load_shutdown_after_complete(cfg)
    manual_default = cfg.get("manual_secs") or ""
    single_sec_default = cfg.get("single_sec") or ""
    single_prompt_default = cfg.get("single_prompt") or ""
    scene_cache = cfg.get("scene_script") or ""
    try:
        preview_scale_default = max(
            160, min(960, int(cfg.get("preview_image_scale") or "420"))
        )
    except ValueError:
        preview_scale_default = 420

    if not srt_default:
        found = find_default_srt(root_default)
        if found is not None:
            srt_default = str(found)
    if not prompt_default:
        found_p = find_image_prompt_file()
        if found_p is not None:
            prompt_default = str(found_p)
    elif not Path(prompt_default).is_file():
        found_p = find_image_prompt_file()
        if found_p is not None:
            prompt_default = str(found_p)

    cred_email, cred_pw = load_credentials()

    def _sync_credentials_to_fields() -> None:
        """슬롯·포트가 바뀌어도 저장된 Genspark 계정을 필드에 유지."""
        nonlocal cred_email, cred_pw
        if not cred_email or not cred_pw:
            cred_email, cred_pw = load_credentials()
        if cred_email and not email_var.get().strip():
            email_var.set(cred_email)
        if cred_pw and not pw_var.get():
            pw_var.set(cred_pw)

    root_var = tk.StringVar(value=root_default)
    png_var = tk.StringVar(value=png_default)
    url_var = tk.StringVar(value=url_default)
    srt_var = tk.StringVar(value=srt_default)
    prompt_var = tk.StringVar(value=prompt_default)
    hourly_retry_var = tk.BooleanVar(
        value=False if script_preview else hourly_retry_default
    )
    prev_ref_var = tk.BooleanVar(value=prev_ref_default)
    session_start_var = tk.StringVar(value=session_start_default)
    limit_reset_var = tk.StringVar(value="정상화 예상: —")
    shutdown_var = tk.BooleanVar(
        value=False if script_preview else shutdown_default
    )
    btn_cancel_wait: ttk.Button | None = None
    manual_var = tk.StringVar(value=manual_default)
    single_sec_var = tk.StringVar(value=single_sec_default)
    single_ref_var = tk.StringVar(value="참조: —")
    email_var = tk.StringVar(value=cred_email)
    pw_var = tk.StringVar(value=cred_pw)
    _sync_credentials_to_fields()
    status_var = tk.StringVar(
        value=(
            f"슬롯 {chrome_slot.index} · CDP :{chrome_slot.port} · "
            f"{chrome_slot.user_data} — 「실행」로 시작"
        )
    )
    scene_var = tk.StringVar(value="")
    busy = {"v": False}
    wait_cancel = {"v": False}
    gen_cancel = threading.Event()
    waiting_limit = {"v": False}
    browser_ready = {"v": False}
    # 브라우저 열기로 입력창에 SRT·프롬프트+명령이 준비됨(미전송)
    input_prepared = {"v": False, "cmd_sec": None}
    scenes: list[SceneLine] = []
    # 씬 정의에 없는데 png 폴더에 있는 PNG (개별 생성 등) — 트리 iid → 임시 SceneLine
    orphan_scenes: dict[str, SceneLine] = {}
    collected: list[tuple[int | None, str]] = []
    scene_text_cache = {"v": scene_cache}
    single_prompt_box: dict[str, tk.Text | None] = {"w": None}
    scene_tree: ttk.Treeview | None = None
    scene_list: tk.Listbox | None = None
    preview_image_lbl: tk.Label | None = None
    preview_scale: ttk.Scale | None = None
    preview_img_wrap: tk.Frame | None = None
    tree_cue_tooltip: dict[str, object] = {"win": None, "iid": None, "after": None}
    preview_thumb_refs: list[object] = []
    # key → (파일 스탬프, PhotoImage). 스탬프가 다르면 덮어쓴 PNG로 보고 다시 읽는다.
    preview_photo_cache: dict[str, tuple[str, object]] = {}
    preview_png_path: dict[str, Path | None] = {"v": None}
    preview_load_token: dict[str, int] = {"n": 0}

    def _read_single_prompt() -> str:
        w = single_prompt_box["w"]
        if w is not None:
            try:
                return w.get("1.0", tk.END).strip()
            except tk.TclError:
                pass
        return single_prompt_default

    def _set_single_prompt(text: str) -> None:
        w = single_prompt_box["w"]
        if w is None:
            return
        try:
            w.delete("1.0", tk.END)
            if text:
                w.insert("1.0", text)
        except tk.TclError:
            pass

    def _script_scene_secs() -> list[int] | None:
        if not script_preview:
            return None
        secs = {int(sc.sec) for sc in scenes}
        secs.update(int(sc.sec) for sc in orphan_scenes.values())
        return sorted(secs)

    def update_single_ref_hint(*_a: object) -> None:
        sec = _parse_single_sec(single_sec_var.get())
        if sec is None:
            single_ref_var.set("참조: —")
            return
        scene_sec_list = _script_scene_secs()
        slot = previous_reference_slot_sec(
            sec,
            interval_sec=_SCENE_INTERVAL_SEC,
            scene_secs=scene_sec_list,
        )
        if slot is None:
            single_ref_var.set("참조: 없음")
            return
        png_dir = Path(png_var.get().strip() or ".")
        ref_path = find_previous_reference_png(
            png_dir,
            sec,
            interval_sec=_SCENE_INTERVAL_SEC,
            scene_secs=scene_sec_list,
        )
        if ref_path is not None:
            single_ref_var.set(f"참조: {ref_path.stem} · 있음")
            return
        tail = " (직전 씬)" if script_preview else f" (t-{_SCENE_INTERVAL_SEC})"
        single_ref_var.set(f"참조: {srt_png_name(slot)}{tail} · 없음")

    frm = ttk.Frame(root, padding=10)
    frm.pack(fill=tk.BOTH, expand=True)
    frm.grid_columnconfigure(0, weight=1)
    frm.grid_rowconfigure(3, weight=1)

    def _selected_scene_index() -> int:
        if script_preview:
            if scene_tree is None:
                return 0
            sel = scene_tree.selection()
            if not sel:
                return 0
            try:
                return max(0, int(sel[0]))
            except ValueError:
                children = list(scene_tree.get_children())
                try:
                    return max(0, children.index(sel[0]))
                except ValueError:
                    return 0
        if scene_list is not None and scene_list.curselection():
            return max(0, int(scene_list.curselection()[0]))
        return 0

    def _selected_scene_sec() -> str:
        sc = selected_scene()
        if sc is not None:
            return str(int(sc.sec))
        return single_sec_var.get().strip()

    def persist() -> None:
        nonlocal cred_email, cred_pw
        save_gui_settings(
            root_dir=root_var.get().strip(),
            png_dir=png_var.get().strip(),
            genspark_url=url_var.get().strip(),
            srt_path=srt_var.get().strip(),
            prompt_path=prompt_var.get().strip(),
            hourly_limit_retry="1" if hourly_retry_var.get() else "0",
            prev_image_reference="1" if prev_ref_var.get() else "0",
            limit_session_start=session_start_var.get().strip(),
            shutdown_after_hours="0",
            shutdown_after_complete="1" if shutdown_var.get() else "0",
            manual_secs=manual_var.get().strip(),
            single_sec=single_sec_var.get().strip(),
            single_prompt=_read_single_prompt(),
            scene_script=scene_text_cache["v"],
            scene_index=str(_selected_scene_index()),
            scene_sec=_selected_scene_sec(),
            preview_image_scale=str(
                int(preview_image_scale_var.get())
                if script_preview
                else preview_scale_default
            ),
        )
        email = email_var.get().strip()
        password = pw_var.get()
        if email and password:
            save_credentials(email, password)
            cred_email, cred_pw = email, password
        elif not email or not password:
            _sync_credentials_to_fields()

    def set_status(msg: str) -> None:
        status_var.set(msg)

    def set_busy(v: bool) -> None:
        busy["v"] = v
        state = tk.DISABLED if v else tk.NORMAL
        for b in (btn_browser, btn_manual, btn_single, btn_refresh):
            try:
                b.configure(state=state)
            except tk.TclError:
                pass
        if not v:
            waiting_limit["v"] = False
            if btn_cancel_wait is not None:
                try:
                    btn_cancel_wait.configure(state=tk.DISABLED)
                except tk.TclError:
                    pass

    def set_limit_reset_display(reset_at: datetime | None) -> None:
        limit_reset_var.set(f"정상화 예상: {format_reset_at(reset_at)}")

    def set_limit_waiting(on: bool) -> None:
        if script_preview:
            return
        waiting_limit["v"] = on
        if btn_cancel_wait is not None:
            try:
                btn_cancel_wait.configure(state=tk.NORMAL if on else tk.DISABLED)
            except tk.TclError:
                pass

    def cancel_limit_wait() -> None:
        if not waiting_limit["v"]:
            return
        wait_cancel["v"] = True
        gen_cancel.set()
        set_status("한도 대기 취소 요청…")

    def cancel_generation(*, reason: str = "사용자 종료") -> None:
        """창 종료·한도 대기 취소 시 이미지 생성·Playwright 세션 중단."""
        if not (busy["v"] or waiting_limit["v"] or gen_cancel.is_set()):
            return
        gen_cancel.set()
        wait_cancel["v"] = True
        browser_ready["v"] = False
        input_prepared["v"] = False
        input_prepared["cmd_sec"] = None
        try:
            reset_image_session()
        except Exception:
            pass
        try:
            close_chrome_debug()
        except Exception:
            pass
        if busy["v"] or waiting_limit["v"]:
            set_status(f"생성 종료 요청 — {reason}")

    def profile_dir() -> Path:
        module_name = (
            "2_7_sceneImageScript" if script_preview else "2_5_sceneImage"
        )
        if getattr(sys, "frozen", False):
            base = Path(sys.executable).resolve().parent
        else:
            base = Path(__file__).resolve().parents[1] / "dist"
            if script_preview:
                alt = Path(__file__).resolve().parents[2] / module_name / "dist"
                if alt.is_dir() or not base.is_dir():
                    base = alt
        if not standalone:
            try:
                from wisdom_root import resolve_wisdom_root

                base = resolve_wisdom_root() / module_name / "dist"
            except Exception:
                pass
        base.mkdir(parents=True, exist_ok=True)
        return image_profile_dir(base)

    def _orphan_iid(sec: int) -> str:
        return f"x{int(sec)}"

    def _saved_png_secs(png_dir: Path) -> set[int]:
        """png 폴더의 SRT_*.png 초 목록 (빈 파일 제외)."""
        out: set[int] = set()
        try:
            for p in png_dir.glob("SRT_*.png"):
                m = re.match(r"SRT_(\d+)\.png$", p.name, re.IGNORECASE)
                if not m:
                    continue
                try:
                    if p.stat().st_size < 512:
                        continue
                except OSError:
                    continue
                out.add(int(m.group(1)))
        except OSError:
            return out
        return out

    def _row_sec(iid: str) -> int:
        try:
            vals = scene_tree.item(iid, "values")
            m = re.match(r"SRT_(\d+)", str(vals[0]))
            return int(m.group(1)) if m else -1
        except (tk.TclError, IndexError, TypeError):
            return -1

    def _mark_scene_png_saved(sec: int) -> None:
        """새로 저장된 PNG를 씬 목록 PNG 열·미리보기에 즉시 반영.

        씬 정의에 없는 초(개별 생성)면 초 순서에 맞춰 행을 새로 끼워 넣는다.
        """
        if not script_preview or scene_tree is None:
            return
        sec = int(sec)
        iid: str | None = None
        for i, sc in enumerate(scenes):
            if int(sc.sec) == sec:
                iid = str(i)
                break
        if iid is None:
            iid = _orphan_iid(sec)
            if not scene_tree.exists(iid):
                pos = len(scene_tree.get_children())
                for n, child in enumerate(scene_tree.get_children()):
                    child_sec = _row_sec(child)
                    if child_sec > sec:
                        pos = n
                        break
                sc_o = SceneLine(sec=sec, prompt="")
                orphan_scenes[iid] = sc_o
                try:
                    scene_tree.insert(
                        "",
                        pos,
                        iid=iid,
                        values=(
                            sc_o.label,
                            _format_scene_time(sec),
                            "",
                            "(씬 없음 · 개별 생성)",
                            "✓",
                        ),
                    )
                except tk.TclError:
                    return
        sc_row = orphan_scenes.get(iid)
        if sc_row is None:
            try:
                sc_row = scenes[int(iid)]
            except (ValueError, IndexError):
                return
        try:
            vals = list(scene_tree.item(iid, "values"))
            if vals and vals[-1] != "✓":
                vals[-1] = "✓"
                scene_tree.item(iid, values=vals)
        except tk.TclError:
            return
        sel = scene_tree.selection()
        if sel and sel[0] == iid:
            _show_scene_png_preview(sc_row, force_reload=True)

    def append_collected_path(sec: int, path: str) -> None:
        label = f"SRT_{sec:03d}"
        short = path if len(path) < 90 else path[:87] + "…"
        if link_list is not None:
            link_list.insert(tk.END, f"{label}  |  {short}")
        collected.append((sec, path))
        _mark_scene_png_saved(sec)

    def apply_root(
        *, force: bool = True, sync_single_fields: bool = True
    ) -> None:
        """루트 지정 시 png·대본(SRT)·이미지프롬프트를 루트 기준으로 맞춘다.

        ``force=False`` 이면 이미 있는 유효한 경로(마지막 입력)를 유지한다.
        """
        raw = root_var.get().strip()
        if not raw:
            return
        r = Path(raw).expanduser()
        layout = ensure_root_layout(r)

        def _ok_dir(path_str: str) -> bool:
            if not path_str.strip():
                return False
            try:
                return Path(path_str).expanduser().is_dir()
            except OSError:
                return False

        def _ok_file(path_str: str) -> bool:
            if not path_str.strip():
                return False
            try:
                return Path(path_str).expanduser().is_file()
            except OSError:
                return False

        if force or not _ok_dir(png_var.get().strip()):
            png_var.set(str(layout["png"]))

        cur_srt = srt_var.get().strip()
        if force or not _ok_file(cur_srt):
            srt = find_default_srt(r)
            if srt is not None:
                srt_var.set(str(srt))
            elif force:
                srt_var.set(str((layout.get("mp3") or (r / "mp3")) / "new.srt"))

        cur_prompt = prompt_var.get().strip()
        if force or not _ok_file(cur_prompt):
            prompt = find_prompt_in_md(r) or find_image_prompt_file(root=r)
            if prompt is not None:
                prompt_var.set(str(prompt))

        reload_scenes(sync_single_fields=sync_single_fields)
        persist()
        set_status(
            f"루트 → png:{Path(png_var.get()).name if png_var.get().strip() else layout['png'].name} · "
            f"srt:{Path(srt_var.get()).name if srt_var.get().strip() else '—'} · "
            f"prompt:{Path(prompt_var.get()).name if prompt_var.get().strip() else '—'} · {r}"
        )

    def auto_assign_from_png(*, force: bool = False) -> None:
        # 호환: png 변경 시 부모를 루트로 간주
        png = png_var.get().strip()
        if not png:
            return
        p = Path(png)
        r = p.parent if p.name.casefold() == "png" else p
        root_var.set(str(r))
        apply_root(force=force)

    def pick_root() -> None:
        init = folder_dialog_initial(
            Path(root_var.get()) if root_var.get().strip() else default_root_dir()
        )
        p = filedialog.askdirectory(parent=root, title="루트 폴더", initialdir=init)
        if p:
            root_var.set(p)
            touch_workspace_from_path(p)
            apply_root(force=True)

    def pick_png() -> None:
        init = folder_dialog_initial(
            Path(png_var.get()) if png_var.get().strip() else default_png_dir()
        )
        p = filedialog.askdirectory(parent=root, title="png 저장 폴더", initialdir=init)
        if p:
            png_var.set(p)
            touch_workspace_from_path(p)
            auto_assign_from_png(force=False)
            set_status(f"png 지정 → {p}")

    def pick_srt() -> None:
        root_p = Path(root_var.get().strip()) if root_var.get().strip() else default_root_dir()
        init = folder_dialog_initial(
            Path(srt_var.get()).parent
            if srt_var.get().strip()
            else (root_p / "mp3" if (root_p / "mp3").is_dir() else root_p / "stt")
        )
        p = filedialog.askopenfilename(
            parent=root,
            title="대본 SRT (new.srt / all.srt)",
            initialdir=init,
            filetypes=[("SRT", "*.srt"), ("모든 파일", "*.*")],
        )
        if p:
            srt_var.set(p)
            reload_scenes()
            persist()

    def pick_prompt() -> None:
        md = module_md_dir()
        md.mkdir(parents=True, exist_ok=True)
        init = folder_dialog_initial(
            Path(prompt_var.get()).parent if prompt_var.get().strip() else md
        )
        p = filedialog.askopenfilename(
            parent=root,
            title="이미지프롬프트 (모듈 md)",
            initialdir=init,
            filetypes=[("텍스트", "*.txt;*.md"), ("모든 파일", "*.*")],
        )
        if p:
            prompt_var.set(p)
            reload_scenes()
            persist()

    # --- paths ---
    path_fr = ttk.LabelFrame(frm, text="경로", padding=(8, 6))
    path_fr.grid(row=0, column=0, sticky="ew", pady=(0, 8))
    path_fr.grid_columnconfigure(1, weight=1)

    ttk.Label(path_fr, text="루트 폴더", width=14).grid(row=0, column=0, sticky="w")
    root_ent = ttk.Entry(path_fr, textvariable=root_var)
    root_ent.grid(row=0, column=1, sticky="ew", padx=(4, 6), pady=2)
    ttk.Button(path_fr, text="찾기…", width=8, command=pick_root).grid(
        row=0, column=2, sticky="e"
    )
    bind_path_entry_dnd(
        root_ent,
        root_var,
        mode="dir",
        on_set=lambda _p: apply_root(force=True),
    )
    bind_path_row_dnd(
        root_ent,
        path_fr,
        root_var,
        mode="dir",
        on_set=lambda _p: apply_root(force=True),
    )

    ttk.Label(path_fr, text="png 폴더", width=14).grid(row=1, column=0, sticky="w")
    png_ent = ttk.Entry(path_fr, textvariable=png_var)
    png_ent.grid(row=1, column=1, sticky="ew", padx=(4, 6), pady=2)
    ttk.Button(path_fr, text="찾기…", width=8, command=pick_png).grid(
        row=1, column=2, sticky="e"
    )
    bind_path_entry_dnd(png_ent, png_var, mode="dir")
    bind_path_row_dnd(png_ent, path_fr, png_var, mode="dir")

    ttk.Label(path_fr, text="대본 (new.srt)", width=14).grid(row=2, column=0, sticky="w")
    srt_ent = ttk.Entry(path_fr, textvariable=srt_var)
    srt_ent.grid(row=2, column=1, sticky="ew", padx=(4, 6), pady=2)
    ttk.Button(path_fr, text="찾기…", width=8, command=pick_srt).grid(
        row=2, column=2, sticky="e"
    )
    bind_path_entry_dnd(srt_ent, srt_var, mode="file")

    ttk.Label(path_fr, text="이미지프롬프트", width=14).grid(row=3, column=0, sticky="w")
    prompt_ent = ttk.Entry(path_fr, textvariable=prompt_var)
    prompt_ent.grid(row=3, column=1, sticky="ew", padx=(4, 6), pady=2)
    ttk.Button(path_fr, text="찾기…", width=8, command=pick_prompt).grid(
        row=3, column=2, sticky="e"
    )
    bind_path_entry_dnd(prompt_ent, prompt_var, mode="file")

    ttk.Checkbutton(
        path_fr,
        text="직전 이미지 참조 첨부",
        variable=prev_ref_var,
        command=persist,
    ).grid(row=4, column=1, columnspan=2, sticky="w", padx=(4, 0), pady=2)

    _url_row = 5 if script_preview else 7
    if not script_preview:
        ttk.Checkbutton(
            path_fr,
            text="한도 시 재설정까지 대기",
            variable=hourly_retry_var,
            command=persist,
        ).grid(row=5, column=1, columnspan=2, sticky="w", padx=(4, 0), pady=2)

        limit_row = ttk.Frame(path_fr)
        limit_row.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(0, 2))
        ttk.Label(limit_row, textvariable=limit_reset_var, width=28).pack(
            side=tk.LEFT
        )
        ttk.Label(limit_row, text="실행 시작(미표시 시)", foreground="#555").pack(
            side=tk.LEFT, padx=(12, 4)
        )
        session_start_ent = ttk.Entry(
            limit_row, textvariable=session_start_var, width=8
        )
        session_start_ent.pack(side=tk.LEFT)
        ttk.Label(limit_row, text="예: 14:30", foreground="#888").pack(
            side=tk.LEFT, padx=(4, 0)
        )
        session_start_var.trace_add("write", lambda *_a: persist())

    ttk.Label(path_fr, text="브라우저 주소", width=14).grid(
        row=_url_row, column=0, sticky="w"
    )
    url_ent = ttk.Entry(path_fr, textvariable=url_var)
    url_ent.grid(row=_url_row, column=1, columnspan=2, sticky="ew", padx=(4, 0), pady=2)

    ttk.Label(path_fr, text="Chrome 계정", width=14).grid(
        row=_url_row + 1, column=0, sticky="w"
    )
    ttk.Entry(path_fr, textvariable=email_var).grid(
        row=_url_row + 1, column=1, columnspan=2, sticky="ew", padx=(4, 0), pady=2
    )
    ttk.Label(path_fr, text="비밀번호(선택)", width=14).grid(
        row=_url_row + 2, column=0, sticky="w"
    )
    ttk.Entry(path_fr, textvariable=pw_var, show="*").grid(
        row=_url_row + 2, column=1, columnspan=2, sticky="ew", padx=(4, 0), pady=2
    )

    def on_root_path_change(*_a: object) -> None:
        p = root_var.get().strip()
        if not p:
            return
        # 존재하는 폴더로 바뀌면 png·srt·프롬프트를 루트 기준으로 맞춤
        try:
            force = Path(p).expanduser().is_dir()
        except OSError:
            force = False
        apply_root(force=force)

    def on_png_path_change(*_a: object) -> None:
        p = png_var.get().strip()
        if p:
            persist()
            touch_workspace_from_path(p)
            if script_preview:
                preview_photo_cache.clear()
                sc = selected_scene()
                if sc is not None:
                    _show_scene_png_preview(sc, force_reload=True)

    png_var.trace_add("write", on_png_path_change)
    root_var.trace_add("write", on_root_path_change)
    srt_var.trace_add("write", lambda *_a: persist())
    prompt_var.trace_add("write", lambda *_a: persist())
    url_var.trace_add("write", lambda *_a: persist())
    manual_var.trace_add("write", lambda *_a: persist())
    single_sec_var.trace_add("write", lambda *_a: persist())

    # --- actions ---
    act = ttk.Frame(frm)
    act.grid(row=1, column=0, sticky="ew", pady=(0, 6))

    def selected_scene() -> SceneLine | None:
        if script_preview and scene_tree is not None:
            sel = scene_tree.selection()
            if sel and sel[0] in orphan_scenes:
                return orphan_scenes[sel[0]]
        i = _selected_scene_index()
        if 0 <= i < len(scenes):
            return scenes[i]
        return None

    def _next_scene_sec(current: int) -> int | None:
        for sec in _script_scene_secs() or [int(sc.sec) for sc in scenes]:
            if sec > int(current):
                return sec
        return None

    def _scene_cue_text(sc: SceneLine) -> str:
        return srt_dialogue_until_next_scene(
            srt_var.get().strip() or None,
            sc.sec,
            _next_scene_sec(sc.sec),
        )

    def refresh_saved_png_list() -> None:
        """png 폴더의 SRT_*.png 목록 갱신 (script 모드는 트리 PNG 열만)."""
        if link_list is not None:
            link_list.delete(0, tk.END)
        collected.clear()
        png_dir = Path(png_var.get().strip() or ".")
        if not png_dir.is_dir():
            return
        found: list[tuple[int, str]] = []
        try:
            for p in sorted(png_dir.glob("SRT_*.png")):
                m = re.match(r"SRT_(\d+)\.png$", p.name, re.IGNORECASE)
                if not m:
                    continue
                try:
                    if p.stat().st_size < 512:
                        continue
                except OSError:
                    continue
                found.append((int(m.group(1)), str(p.resolve())))
        except OSError:
            return
        for sec, path in sorted(found, key=lambda x: x[0]):
            append_collected_path(sec, path)
        if script_preview and scene_tree is not None:
            for i, sc in enumerate(scenes):
                png_mark = "✓" if png_already_exists(png_dir, sc.sec) else "—"
                try:
                    vals = list(scene_tree.item(str(i), "values"))
                    if vals:
                        vals[-1] = png_mark
                        scene_tree.item(str(i), values=vals)
                except tk.TclError:
                    pass

    def refresh_chapter_settings() -> None:
        """현재 장(루트)의 png·SRT·이미지프롬프트·씬·characters.json 을 디스크에서 다시 읽는다."""
        if busy["v"]:
            safe_messagebox(
                root,
                "showinfo",
                "2_5 sceneImage",
                "작업 중에는 새로고침할 수 없습니다.",
            )
            return
        raw_root = root_var.get().strip()
        if not raw_root:
            safe_messagebox(
                root,
                "showwarning",
                "2_5 sceneImage",
                "루트 폴더를 먼저 지정하세요.",
            )
            return
        scene_text_cache["v"] = ""
        input_prepared["v"] = False
        input_prepared["cmd_sec"] = None
        browser_ready["v"] = False
        try:
            clear_registry_cache()
        except Exception:
            pass
        try:
            reset_image_session()
        except Exception:
            pass
        try:
            apply_root(force=True)
        except Exception as ex:
            safe_messagebox(root, "showerror", "2_5 sceneImage", str(ex))
            return
        refresh_saved_png_list()
        update_single_ref_hint()
        persist()
        n_scenes = len(scenes)
        n_saved = link_list.size() if link_list is not None else len(collected)
        set_status(
            f"새로고침 — 씬 {n_scenes}개 · 저장 PNG {n_saved}개 · "
            f"srt:{Path(srt_var.get()).name if srt_var.get().strip() else '—'} · "
            f"prompt:{Path(prompt_var.get()).name if prompt_var.get().strip() else '—'}"
        )

    def reload_scenes(*, sync_single_fields: bool = True) -> None:
        nonlocal scenes
        text = load_scene_text(
            prompt_path=prompt_var.get().strip() or None,
            png_dir=png_var.get().strip() or None,
            fallback_text=scene_text_cache["v"],
        )
        if text.strip():
            scene_text_cache["v"] = text
        parsed = parse_scene_script(text)
        character_names: list[str] = []
        if script_preview:
            registry = get_registry(
                prompt_path=prompt_var.get().strip() or None,
                png_dir=png_var.get().strip() or None,
            )
            for character in registry.characters.values():
                character_names.extend(character.detect)
        scenes = build_interval_scenes(
            parsed,
            srt_path=srt_var.get().strip() or None,
            interval_sec=_SCENE_INTERVAL_SEC,
            character_names=character_names,
            auto_transition_cuts=script_preview,
        )
        png_dir = Path(png_var.get().strip() or ".")
        srt_path = srt_var.get().strip() or None
        if script_preview:
            for item in scene_tree.get_children():
                scene_tree.delete(item)
            orphan_scenes.clear()
            rows: list[tuple[int, str, SceneLine]] = [
                (sc.sec, str(i), sc) for i, sc in enumerate(scenes)
            ]
            scene_secs = {int(sc.sec) for sc in scenes}
            for sec in sorted(_saved_png_secs(png_dir) - scene_secs):
                iid = _orphan_iid(sec)
                sc_o = SceneLine(sec=sec, prompt="")
                orphan_scenes[iid] = sc_o
                rows.append((sec, iid, sc_o))
            rows.sort(key=lambda r: r[0])
            for sec, iid, sc in rows:
                cue = srt_dialogue_until_next_scene(
                    srt_path,
                    sc.sec,
                    _next_scene_sec(sc.sec),
                )
                cue_disp = cue.replace("\n", " ").strip()
                if len(cue_disp) > 120:
                    cue_disp = cue_disp[:117] + "…"
                png_mark = "✓" if png_already_exists(png_dir, sc.sec) else "—"
                if iid in orphan_scenes:
                    cue_disp = f"(씬 없음 · 개별 생성) {cue_disp}".strip()
                reason = sc.cut_reason if sc.cut_kind == "transition" else ""
                scene_tree.insert(
                    "",
                    tk.END,
                    iid=iid,
                    values=(
                        sc.label,
                        _format_scene_time(sc.sec),
                        reason,
                        cue_disp or "—",
                        png_mark,
                    ),
                )
        else:
            scene_list.delete(0, tk.END)
            for sc in scenes:
                mark = "✓ " if png_already_exists(png_dir, sc.sec) else ""
                scene_list.insert(tk.END, f"{mark}{sc.list_label()}")
        if scenes:
            idx = 0
            raw = cfg.get("scene_index", "0")
            try:
                idx = max(0, min(len(scenes) - 1, int(raw)))
            except ValueError:
                idx = 0
            want_sec: int | None = None
            raw_sec = (cfg.get("scene_sec") or "").strip()
            if raw_sec.isdigit():
                want_sec = int(raw_sec)
            elif single_sec_default.strip().isdigit():
                want_sec = int(single_sec_default.strip())
            if script_preview and scene_tree is not None:
                iid = str(idx)
                if want_sec is not None:
                    for child in scene_tree.get_children():
                        if _row_sec(child) == want_sec:
                            iid = child
                            break
                scene_tree.selection_set(iid)
                scene_tree.see(iid)
            else:
                scene_list.selection_set(idx)
            if sync_single_fields:
                on_scene_select()
            elif script_preview:
                sc = selected_scene()
                if sc is not None:
                    _show_scene_png_preview(sc)
            set_status(
                f"씬 {len(scenes)}개 · {scenes[0].label}…{scenes[-1].label}"
            )
        else:
            scene_var.set("")
            set_status(
                "생성할 씬이 없습니다. 이미지프롬프트·SRT(new.srt)를 확인하세요."
            )
        persist()

    def _account() -> tuple[str, str] | None:
        _sync_credentials_to_fields()
        email = email_var.get().strip()
        password = pw_var.get()
        if not email or not password:
            _e, _p = load_credentials()
            if not email and _e:
                email = _e
                email_var.set(_e)
            if not password and _p:
                password = _p
                pw_var.set(_p)
        if not email:
            safe_messagebox(
                root, "showwarning", "2_5 sceneImage", "계정 이메일을 입력하세요."
            )
            return None
        return email, password

    def _ensure_browser_and_paste(
        *,
        email: str,
        password: str,
        url: str,
        model_sel: str,
        model_texts: list[str] | tuple[str, ...],
        pipe: dict,
        png_dir: Path,
        force_paste: bool = True,
        first_command_sec: int | None = None,
        first_scene_prompt: str | None = None,
        submit_context: bool = True,
        force_reopen: bool = False,
        attach_reference_override: bool | None = None,
        first_next_scene_sec: int | None = None,
        skip_fresh_composer: bool = False,
        open_model_light: bool = False,
    ) -> tuple[object, bool]:
        """브라우저 오픈 + 프롬프트/대본 붙여넣기 (+ 선택: 첫 명령 입력).

        ``force_reopen=True`` 이면 기존 ChromeDebug·세션을 종료하고 새로 연다.
        ``open_model_light=True`` — script 개별: 탭 리셋·75k 붙여넣기 없이 로그인·모델만.
        """
        if force_reopen:
            browser_ready["v"] = False
            clear_chrome_session_restore()
            info = open_browser_for_account(
                url, email=email, restart_chrome=True
            )
            # CDP 준비는 open_chrome_debug 내부에서 대기함
            time.sleep(0.5)
            append_image_log(
                png_dir,
                f"ChromeDebug 재시작 slot={info.get('slot')} "
                f"port={info.get('debug_port')} "
                f"user_data={info.get('user_data')} reused={info.get('reused')}",
            )
        elif not browser_ready["v"]:
            clear_chrome_session_restore()
            info = open_browser_for_account(url, email=email)
            time.sleep(0.5)
            append_image_log(
                png_dir,
                f"ChromeDebug 열림 slot={info.get('slot')} "
                f"port={info.get('debug_port')} "
                f"user_data={info.get('user_data')} reused={info.get('reused')}",
            )
        if not has_playwright():
            raise RuntimeError("Playwright가 필요합니다.")

        sess = get_image_session(profile_dir())
        opened_light = False
        if browser_ready["v"] and not force_reopen and not skip_fresh_composer:
            try:
                sess.ensure_fresh_composer(url=url)
                append_image_log(
                    png_dir,
                    "AI Image 입력창 새로 열기 (매직 다시 그리기 방지)",
                )
            except Exception as fresh_ex:
                append_image_log(
                    png_dir,
                    f"입력창 새로 열기 경고: {fresh_ex}",
                )
        if not browser_ready["v"]:
            opened_light = bool(open_model_light)
            safe_after(
                root,
                lambda: set_status(
                    "Genspark 연결·로그인…"
                    if open_model_light
                    else "Genspark 페이지 연결·로그인…"
                ),
            )
            result = sess.open_and_select_model(
                url=url,
                model_selector=model_sel,
                email=email,
                password=password,
                model_texts=model_texts,
                light=open_model_light,
            )
            logged_in = bool(isinstance(result, dict) and result.get("logged_in"))
            model_ok = bool(isinstance(result, dict) and result.get("model_auto"))
            append_image_log(
                png_dir,
                f"세션 준비 — login={'OK' if logged_in else '실패(Chrome에서 수동 로그인 필요)'} "
                f"model={pipe.get('model')} auto={model_ok}"
                + (" · light" if open_model_light else ""),
            )
            if not logged_in:
                safe_after(
                    root,
                    lambda: set_status(
                        "로그인 실패 — Chrome에서 Genspark에 직접 로그인하세요"
                    ),
                )
            browser_ready["v"] = True
            if opened_light:
                append_image_log(
                    png_dir,
                    "script 준비 — SRT·프롬프트 붙여넣기 생략 "
                    "(run_scene에서 명령·참조 첨부)",
                )
        else:
            model_ok = True

        # 붙여넣기 직전: 없는 SRT/프롬프트 경로를 루트·모듈에서 재탐색
        prompt_path = prompt_var.get().strip()
        srt_path = srt_var.get().strip()
        if not prompt_path or not Path(prompt_path).is_file():
            found_p = find_image_prompt_file(preferred=prompt_path or None)
            if found_p is not None:
                prompt_path = str(found_p)
                prompt_var.set(prompt_path)
        if not srt_path or not Path(srt_path).is_file():
            found_s = find_default_srt(root_var.get().strip() or ".")
            if found_s is not None:
                srt_path = str(found_s)
                srt_var.set(srt_path)
                append_image_log(png_dir, f"SRT 경로 재지정 → {srt_path}")

        stats = paste_payload_stats(prompt_path, srt_path)
        paste = build_paste_payload(prompt_path, srt_path)
        if force_paste:
            if not stats["prompt_ok"]:
                raise RuntimeError(
                    "이미지프롬프트 파일을 읽을 수 없습니다.\n"
                    f"경로: {prompt_path or '(비어 있음)'}"
                )
            if not stats["srt_ok"]:
                raise RuntimeError(
                    "SRT 파일을 읽을 수 없습니다. (프롬프트만 붙여넣히던 원인)\n"
                    f"경로: {srt_path or '(비어 있음)'}\n"
                    "루트/mp3 또는 stt 의 new.srt · all.srt 를 확인하세요."
                )
            if not stats["has_srt_timecode"]:
                raise RuntimeError(
                    f"SRT에 타임코드(-->)가 없습니다.\n{srt_path}"
                )
            safe_after(
                root,
                lambda: set_status(
                    f"SRT·프롬프트 붙여넣기 중… ({stats['prompt_chars']}+{stats['srt_chars']}자)"
                ),
            )
            # 동일 입력창에 SRT·프롬프트만 붙여넣기 (실행은 SRT_XXX 명령에서)
            if submit_context:
                sess.submit_prompt(
                    url=url,
                    prompt=paste,
                    model_selector=model_sel,
                    try_model_select=not model_ok,
                    model_texts=model_texts,
                )
            else:
                sess.paste_text(
                    url=url,
                    text=paste,
                    model_selector=model_sel,
                    try_model_select=not model_ok,
                    model_texts=model_texts,
                )
            append_image_log(
                png_dir,
                f"입력창 붙여넣기 — 프롬프트 {stats['prompt_chars']}자"
                f" + SRT {stats['srt_chars']}자"
                f" = 합계 {len(paste)}자"
                f"\n  prompt={prompt_path}\n  srt={srt_path}",
            )
            time.sleep(1.0)

        if first_command_sec is not None:
            prep_ref_path = None
            want_prep_ref = (
                attach_reference_override
                if attach_reference_override is not None
                else bool(prev_ref_var.get())
            )
            if want_prep_ref:
                prep_ref_path = find_previous_reference_png(
                    png_dir,
                    first_command_sec,
                    interval_sec=_SCENE_INTERVAL_SEC,
                    scene_secs=_script_scene_secs(),
                )
            cmd = build_generate_command_from_sources(
                first_command_sec,
                scene_prompt=first_scene_prompt,
                srt_path=srt_path,
                png_dir=png_dir,
                prompt_path=prompt_path or None,
                reference_attached=prep_ref_path is not None,
                reference_label=(
                    prep_ref_path.stem if prep_ref_path is not None else ""
                ),
                next_scene_sec=first_next_scene_sec,
            )
            # SRT·프롬프트가 들어 있는 동일 입력창 끝에 명령만 추가 (실행 안 함)
            sess.paste_text(
                url=url,
                text=cmd,
                model_selector=model_sel,
                try_model_select=False,
                model_texts=model_texts,
                append=True,
            )
            append_image_log(
                png_dir,
                f"동일 입력창에 명령 추가(미실행): {cmd[:180]}"
                + ("…" if len(cmd) > 180 else ""),
            )
            input_prepared["v"] = True
            input_prepared["cmd_sec"] = int(first_command_sec)
            time.sleep(0.5)
        return sess, model_ok

    def _run_scenes(
        todo: list[SceneLine],
        *,
        open_browser_first: bool,
        title: str,
        force_reopen: bool = False,
        force_regenerate: bool = False,
        attach_reference_override: bool | None = None,
        dialogue_until_next_scene: bool = False,
        script_light_run: bool = False,
    ) -> None:
        if busy["v"] and not force_reopen:
            safe_messagebox(
                root,
                "showinfo",
                "2_5 sceneImage",
                "이미 작업 중입니다. 끝난 뒤 「실행」를 다시 누르세요.",
            )
            return
        acc = _account()
        if acc is None:
            return
        email, password = acc
        png_dir = Path(png_var.get().strip())
        if not str(png_dir):
            safe_messagebox(root, "showwarning", "2_5 sceneImage", "png 폴더를 지정하세요.")
            return
        if not todo:
            safe_messagebox(
                root, "showinfo", "2_5 sceneImage", "생성할 씬이 없거나 모두 이미 있습니다."
            )
            return
        pipe = load_pipeline_config()
        url = preferred_genspark_url(
            url_var.get().strip() or str(pipe.get("genspark_url") or "")
        )
        model_sel = load_model_selector()
        model_texts = model_name_variants(str(pipe.get("model") or "Nano Banana Pro"))
        do_limit_wait = bool(hourly_retry_var.get()) and not script_preview
        gen_timeout = max(120, int(pipe.get("generate_timeout_sec") or 120))
        first_sec = int(todo[0].sec)
        if force_regenerate and not script_light_run:
            input_prepared["v"] = False
            input_prepared["cmd_sec"] = None
        # script 개별: 브라우저·SRT 컨텍스트 있으면 run_scene만 (75k 재붙여넣기 생략)
        if script_light_run and browser_ready["v"] and not force_reopen:
            need_prepare = False
        else:
            need_prepare = (
                force_reopen
                or (force_regenerate and not script_light_run)
                or open_browser_first
                or (not browser_ready["v"])
                or (not input_prepared["v"])
                or (input_prepared.get("cmd_sec") != first_sec)
            )
        skip_context_paste = bool(
            script_light_run and dialogue_until_next_scene
        )
        persist()
        wait_cancel["v"] = False
        gen_cancel.clear()

        def _session_start_hm() -> tuple[int, int] | None:
            return parse_session_start_hm(session_start_var.get())

        def _recover_reset_at_via_browser(*, reason: str) -> datetime | None:
            """정상화 시각 미확인 시 브라우저 1회 재오픈 후 배너 probe (최대 3회)."""
            max_tries = 3
            browser_opened = False
            sess = get_image_session(profile_dir())
            for attempt in range(1, max_tries + 1):
                if (
                    wait_cancel["v"]
                    or gen_cancel.is_set()
                    or not hourly_retry_var.get()
                ):
                    return None
                append_image_log(
                    png_dir,
                    f"정상화 시각 미확인 — 배너 확인 "
                    f"({attempt}/{max_tries}) — {reason}",
                )
                safe_after(
                    root,
                    lambda a=attempt: set_status(
                        f"한도 — 정상화 시각 확인 ({a}/{max_tries})"
                    ),
                )
                try:
                    if attempt == 1 or not browser_opened:
                        try:
                            close_chrome_debug()
                        except Exception as close_ex:
                            append_image_log(
                                png_dir,
                                f"정상화 확인 전 브라우저 종료 경고: {close_ex}",
                            )
                        browser_ready["v"] = False
                        input_prepared["v"] = False
                        input_prepared["cmd_sec"] = None
                        try:
                            from scene_image.genspark_image import (
                                reset_image_session,
                            )

                            reset_image_session()
                        except Exception:
                            pass
                        time.sleep(1.0)
                        info = open_browser_for_account(
                            url, email=email, restart_chrome=False
                        )
                        append_image_log(
                            png_dir,
                            f"정상화 확인용 ChromeDebug "
                            f"port={info.get('debug_port')} attempt={attempt}",
                        )
                        time.sleep(0.5)
                        sess = get_image_session(profile_dir())
                        sess.open_and_select_model(
                            url=url,
                            model_selector=model_sel,
                            email=email,
                            password=password,
                            model_texts=model_texts,
                        )
                        browser_opened = True
                        browser_ready["v"] = True
                    else:
                        time.sleep(2.0)
                    probed = sess.probe_limit_reset(
                        url=url, email=email, password=password
                    )
                    snip = ((probed or {}).get("snippet") or "")[:200]
                    parsed = _reset_at_from_probe(probed)
                    append_image_log(
                        png_dir,
                        f"배너 probe reset_at="
                        f"{parsed.strftime('%Y-%m-%d %H:%M') if parsed else '-'} "
                        f"is_limit={(probed or {}).get('is_limit')} "
                        f"snip={snip!r}",
                    )
                    if parsed is not None:
                        return parsed
                except Exception as probe_ex:
                    append_image_log(
                        png_dir,
                        f"정상화 시각 배너 확인 실패 ({attempt}/{max_tries}): {probe_ex}",
                    )
                    browser_opened = False
                    browser_ready["v"] = False
            if browser_opened:
                browser_ready["v"] = True
            return None

        def _wait_until_reset(*, reset_at: datetime, reason: str) -> bool:
            """재설정 시각까지 대기. True=재개, False=취소."""
            now = datetime.now()
            wait_sec = int((reset_at - now).total_seconds()) + _LIMIT_RESET_BUFFER_SEC
            wait_sec = max(30, wait_sec)
            chunk = _LIMIT_WAIT_CHUNK_SEC
            label = format_reset_at(reset_at)
            append_image_log(
                png_dir,
                f"한도 대기 시작 — 정상화 예상 {label} ({wait_sec // 60}분) — {reason}",
            )
            safe_after(root, lambda: set_limit_reset_display(reset_at))
            safe_after(root, lambda: set_limit_waiting(True))
            elapsed = 0
            while elapsed < wait_sec:
                if (
                    wait_cancel["v"]
                    or gen_cancel.is_set()
                    or not hourly_retry_var.get()
                ):
                    safe_after(root, lambda: set_limit_waiting(False))
                    append_image_log(png_dir, "한도 대기 취소됨")
                    return False
                left = wait_sec - elapsed
                h, rem = divmod(left, 3600)
                m, s = divmod(rem, 60)
                safe_after(
                    root,
                    lambda hh=h, mm=m, ss=s, r=reason, lbl=label: set_status(
                        f"한도 대기 {hh:d}:{mm:02d}:{ss:02d} — "
                        f"정상화 예상 {lbl} — {r}"
                    ),
                )
                time.sleep(min(chunk, left))
                elapsed += chunk
            safe_after(root, lambda: set_limit_waiting(False))
            append_image_log(
                png_dir, f"한도 대기 종료 — 정상화 예상 {label} — 재시도"
            )
            return True

        def _resolve_reset_at(
            err: BaseException | str,
            *,
            page_snip: str = "",
            pre_reset: datetime | None = None,
        ) -> datetime | None:
            if pre_reset is not None:
                return pre_reset
            session_hm = _session_start_hm()
            combined: BaseException | str = err
            if page_snip:
                if isinstance(err, BaseException):
                    combined = f"{err}\n{page_snip}"
                else:
                    combined = f"{err}\n{page_snip}"
            return resolve_limit_reset_at(
                combined, session_start_hm=session_hm
            )

        def _wait_for_limit(
            reason: str,
            err: BaseException | str,
            *,
            page_snip: str = "",
            pre_reset: datetime | None = None,
        ) -> bool:
            """재설정 시각까지 대기. 배너 시각이 없으면 브라우저 재오픈으로 확인."""
            session_hm = _session_start_hm()
            reset_at = _resolve_reset_at(
                err, page_snip=page_snip, pre_reset=pre_reset
            )
            if reset_at is None:
                reset_at = _recover_reset_at_via_browser(reason=reason)
            if reset_at is None and session_hm:
                # GUI에 수동 입력된 실행 시작이 있으면 최후 보조
                reset_at = resolve_limit_reset_at(
                    err, session_start_hm=session_hm
                )
            if reset_at is None:
                append_image_log(
                    png_dir,
                    "한도 대기 불가 — 브라우저 재오픈으로도 정상화 시각 확인 실패",
                )
                safe_after(
                    root,
                    lambda: set_status(
                        "한도 — 정상화 시각을 배너에서 읽지 못했습니다"
                    ),
                )
                return False
            safe_after(root, lambda ra=reset_at: set_limit_reset_display(ra))
            return _wait_until_reset(reset_at=reset_at, reason=reason)

        def work() -> None:
            try:
                if not has_playwright():
                    raise RuntimeError(
                        "Playwright가 없습니다. 수동으로 생성하세요."
                    )
                set_tab_log_png_dir(png_dir)
                append_image_log(
                    png_dir,
                    f"탭로그 ON — {title} · 씬 {len(todo)}개 · reopen={force_reopen}"
                    + (" · light" if script_light_run and not need_prepare else "")
                    + (
                        " · script-prep(로그인만)"
                        if skip_context_paste and need_prepare
                        else ""
                    )
                    + (f" · 한도대기ON" if do_limit_wait else ""),
                )
                first_next = (
                    _next_scene_sec(first_sec)
                    if dialogue_until_next_scene
                    else None
                )
                # 개별·강제 재생성: SRT·프롬프트만 붙이고 명령·참조는 run_scene에서
                prep_command = need_prepare and not force_regenerate
                sess, model_ok = _ensure_browser_and_paste(
                    email=email,
                    password=password,
                    url=url,
                    model_sel=model_sel,
                    model_texts=model_texts,
                    pipe=pipe,
                    png_dir=png_dir,
                    force_paste=need_prepare and not skip_context_paste,
                    first_command_sec=first_sec if prep_command else None,
                    first_scene_prompt=(
                        todo[0].prompt if prep_command and todo else None
                    ),
                    # 컨텍스트+명령은 입력만 — 전송은 아래 run_scene(첫 씬)
                    submit_context=False,
                    force_reopen=force_reopen,
                    attach_reference_override=attach_reference_override,
                    first_next_scene_sec=first_next if prep_command else None,
                    skip_fresh_composer=bool(
                        script_light_run and not need_prepare
                    ),
                    open_model_light=bool(
                        skip_context_paste and need_prepare
                    ),
                )
                saved_n = 0
                skipped_n = 0
                ran_n = 0
                failed_n = 0
                fail_streak = 0
                cancelled_wait = False
                user_cancelled = False
                browser_aborted = False
                remaining = list(todo)
                total_n = len(todo)
                done_secs: list[int] = []

                while remaining:
                    if gen_cancel.is_set():
                        user_cancelled = True
                        append_image_log(
                            png_dir,
                            f"생성 중단 — 남은 씬 {len(remaining)}개 "
                            f"(창 종료·취소)",
                        )
                        break
                    sc = remaining[0]
                    if not force_regenerate and png_already_exists(png_dir, sc.sec):
                        skipped_n += 1
                        path = str(scene_png_path(png_dir, sc.sec))
                        append_image_log(
                            png_dir, f"{sc.label} 건너뜀 (기존 PNG) {path}"
                        )

                        def _skip(s=sc, p=path) -> None:
                            append_collected_path(s.sec, p)

                        safe_after(root, _skip)
                        done_secs.append(int(sc.sec))
                        remaining.pop(0)
                        continue
                    sc_next = (
                        _next_scene_sec(sc.sec)
                        if dialogue_until_next_scene
                        else None
                    )
                    cmd = build_generate_command_from_sources(
                        sc.sec,
                        scene_prompt=sc.prompt,
                        srt_path=srt_var.get().strip() or None,
                        interval_sec=_SCENE_INTERVAL_SEC,
                        png_dir=png_dir,
                        prompt_path=prompt_var.get().strip() or None,
                        next_scene_sec=sc_next,
                    )
                    # 첫 실행·한도 재오픈 후: 입력창에 준비된 명령이 이 씬이면 그대로 전송
                    use_box = (
                        not force_regenerate
                        and bool(input_prepared["v"])
                        and input_prepared.get("cmd_sec") == int(sc.sec)
                    )
                    done_i = total_n - len(remaining) + 1
                    safe_after(
                        root,
                        lambda s=sc, n=done_i, c=cmd, u=use_box: set_status(
                            f"{title} {n}/{total_n} — "
                            + ("입력창 전송 · " if u else "")
                            + c[:80]
                            + ("…" if len(c) > 80 else "")
                        ),
                    )
                    attach_ref = (
                        attach_reference_override
                        if attach_reference_override is not None
                        else bool(prev_ref_var.get())
                    )
                    try:
                        out = sess.run_scene_with_retry(
                            url=url,
                            prompt=sc.prompt,
                            png_dir=png_dir,
                            srt_sec=sc.sec,
                            model_selector=model_sel,
                            model_texts=model_texts,
                            try_model_select=(ran_n == 0),
                            retry_count=1,
                            retry_wait_sec=0,
                            generate_timeout_sec=gen_timeout,
                            use_existing_input=use_box,
                            srt_path=srt_var.get().strip() or None,
                            interval_sec=_SCENE_INTERVAL_SEC,
                            prompt_path=prompt_var.get().strip() or None,
                            attach_reference=attach_ref,
                            prior_secs=list(done_secs),
                            email=email,
                            password=password,
                            force_regenerate=force_regenerate,
                            next_scene_sec=sc_next,
                            scene_secs=_script_scene_secs(),
                        )
                    except Exception as scene_err:
                        failed_n += 1
                        fail_streak += 1
                        if use_box:
                            input_prepared["v"] = False
                            input_prepared["cmd_sec"] = None
                        ran_n += 1
                        err_s = str(scene_err)
                        if is_browser_closed_error(scene_err):
                            browser_aborted = True
                            browser_ready["v"] = False
                            input_prepared["v"] = False
                            input_prepared["cmd_sec"] = None
                            append_fail_log(
                                png_dir,
                                scene=sc.label,
                                error=err_s,
                                kind="browser_closed",
                                extra="생성 즉시 중단",
                            )
                            append_image_log(
                                png_dir,
                                f"브라우저 종료 — {sc.label}에서 생성 중단 "
                                f"(남은 씬 {len(remaining)}개 스킵)\n{err_s}",
                            )
                            safe_after(
                                root,
                                lambda s=sc: set_status(
                                    f"브라우저 종료 — {s.label}에서 중단"
                                ),
                            )
                            break
                        is_limit = isinstance(scene_err, AiImageLimitError) or (
                            _looks_like_limit_error(err_s)
                        )
                        limit_hit = is_limit
                        # 실패 분석 로그
                        kind = "limit" if is_limit else "fail"
                        extra = f"streak={fail_streak}"
                        if fail_streak >= _LIMIT_FAIL_STREAK and not is_limit:
                            extra += " (한도 아님 — Chrome 유지)"
                        if isinstance(scene_err, AiImageLimitError):
                            if scene_err.reset_at:
                                extra += (
                                    " reset_at="
                                    + scene_err.reset_at.strftime("%Y-%m-%d %H:%M")
                                )
                            page_snip = scene_err.raw or ""
                        else:
                            page_snip = ""
                        append_fail_log(
                            png_dir,
                            scene=sc.label,
                            error=err_s,
                            kind=kind,
                            page_snip=page_snip,
                            extra=extra,
                        )
                        if do_limit_wait and limit_hit and hourly_retry_var.get():
                            known_reset = _resolve_reset_at(
                                scene_err,
                                page_snip=page_snip or err_s,
                                pre_reset=(
                                    scene_err.reset_at
                                    if isinstance(
                                        scene_err, AiImageLimitError
                                    )
                                    and scene_err.reset_at
                                    else None
                                ),
                            )
                            if known_reset is None:
                                append_image_log(
                                    png_dir,
                                    f"{sc.label} 한도 — 정상화 시각 미확인, "
                                    "브라우저 종료 전 배너 probe",
                                )
                                try:
                                    probed = sess.probe_limit_reset(
                                        url=url,
                                        email=email,
                                        password=password,
                                    )
                                    known_reset = _reset_at_from_probe(probed)
                                    snip = (
                                        (probed or {}).get("snippet") or ""
                                    )[:200]
                                    append_image_log(
                                        png_dir,
                                        f"한도 probe(종료 전) "
                                        f"reset_at="
                                        f"{known_reset.strftime('%Y-%m-%d %H:%M') if known_reset else '-'} "
                                        f"is_limit={(probed or {}).get('is_limit')} "
                                        f"snip={snip!r}",
                                    )
                                except Exception as probe_ex:
                                    append_image_log(
                                        png_dir,
                                        f"한도 probe(종료 전) 실패: {probe_ex}",
                                    )
                            if known_reset is not None:
                                safe_after(
                                    root,
                                    lambda ra=known_reset: set_limit_reset_display(
                                        ra
                                    ),
                                )
                            append_image_log(
                                png_dir,
                                f"{sc.label} 실패(한도 "
                                f"{'확정' if is_limit else '추정'} "
                                f"streak={fail_streak}) "
                                f"— 브라우저 종료 후 대기·재오픈\n{err_s}",
                            )
                            safe_after(
                                root,
                                lambda s=sc, e=err_s, lbl=(
                                    format_reset_at(known_reset)
                                    if known_reset
                                    else "—"
                                ): set_status(
                                    f"{s.label} 한도 — 정상화 예상 {lbl} — "
                                    f"브라우저 종료·대기 — {e[:40]}"
                                ),
                            )
                            # 한도 대기 전: 이미지용 브라우저·세션 종료
                            try:
                                close_chrome_debug()
                                append_image_log(
                                    png_dir, "한도 대기 전 ChromeDebug 종료"
                                )
                            except Exception as close_ex:
                                append_image_log(
                                    png_dir, f"브라우저 종료 경고: {close_ex}"
                                )
                            browser_ready["v"] = False
                            input_prepared["v"] = False
                            input_prepared["cmd_sec"] = None
                            if not _wait_for_limit(
                                f"{sc.label} 한도",
                                scene_err,
                                page_snip=page_snip or err_s,
                                pre_reset=known_reset,
                            ):
                                cancelled_wait = True
                                break
                            # 재개: 「실행」와 동일 — 재오픈·로그인·붙여넣기·이 씬 명령
                            append_image_log(
                                png_dir,
                                f"한도 대기 종료 — 브라우저 재오픈 후 {sc.label} 재개",
                            )
                            safe_after(
                                root,
                                lambda s=sc: set_status(
                                    f"한도 해제 추정 — 브라우저 재오픈 · {s.label}"
                                ),
                            )
                            try:
                                sess, model_ok = _ensure_browser_and_paste(
                                    email=email,
                                    password=password,
                                    url=url,
                                    model_sel=model_sel,
                                    model_texts=model_texts,
                                    pipe=pipe,
                                    png_dir=png_dir,
                                    force_paste=True,
                                    first_command_sec=int(sc.sec),
                                    first_scene_prompt=sc.prompt,
                                    submit_context=False,
                                    force_reopen=True,
                                )
                            except Exception as reopen_ex:
                                append_fail_log(
                                    png_dir,
                                    scene=sc.label,
                                    error=str(reopen_ex),
                                    kind="reopen_fail",
                                    extra="한도 대기 후 브라우저 재오픈 실패",
                                )
                                append_image_log(
                                    png_dir,
                                    f"재오픈 실패 — 중단\n{reopen_ex}",
                                )
                                cancelled_wait = True
                                break
                            fail_streak = 0
                            continue
                        # 한도 재시도 OFF 또는 한도 아님 → 다음 씬
                        remaining.pop(0)
                        append_image_log(
                            png_dir,
                            f"{sc.label} 실패 — 다음 씬 계속\n{err_s}",
                        )
                        safe_after(
                            root,
                            lambda s=sc, e=err_s: set_status(
                                f"{s.label} 실패 · 다음 씬 계속 — {e[:80]}"
                            ),
                        )
                        continue
                    if use_box:
                        input_prepared["v"] = False
                        input_prepared["cmd_sec"] = None
                    ran_n += 1
                    fail_streak = 0
                    remaining.pop(0)
                    done_secs.append(int(sc.sec))
                    paths = list((out or {}).get("saved") or [])
                    saved_n += len(paths)
                    show_paths = paths or [str(scene_png_path(png_dir, sc.sec))]
                    ref_note = ""
                    if (out or {}).get("reference_attached"):
                        ref_note = (
                            f"\n참조 첨부: {(out or {}).get('reference_file') or 'OK'}"
                        )
                    append_image_log(
                        png_dir,
                        f"{sc.label} 생성·다운로드 완료{ref_note}\n"
                        + "\n".join(show_paths),
                    )

                    def _done_paths(s=sc, ps=list(show_paths)) -> None:
                        for p in ps:
                            append_collected_path(s.sec, p)

                    safe_after(root, _done_paths)

                # 마지막 씬까지 끝난 뒤: 늦은 이미지 회수 + PNG 존재로 실패 최종 확인
                fail_labels: list[str] = []
                recovered_n = 0
                if not cancelled_wait and not browser_aborted and not user_cancelled:
                    missing_secs = [
                        int(sc.sec)
                        for sc in todo
                        if not png_already_exists(png_dir, sc.sec)
                    ]
                    if missing_secs:
                        safe_after(
                            root,
                            lambda n=len(missing_secs): set_status(
                                f"{title} 완료 전 — 다운로드 실패 {n}건 확인·회수…"
                            ),
                        )
                        try:
                            salvage = sess.salvage_pending(
                                png_dir=png_dir, secs=missing_secs
                            ) or {}
                            recovered_n = int(salvage.get("recovered") or 0)
                            for p in list(salvage.get("saved") or []):
                                try:
                                    name = Path(str(p)).name
                                    m = re.match(
                                        r"SRT_(\d+)\.png$", name, re.IGNORECASE
                                    )
                                    sec_r = int(m.group(1)) if m else -1
                                except Exception:
                                    sec_r = -1

                                def _salvaged(sec=sec_r, path=str(p)) -> None:
                                    if sec >= 0:
                                        append_collected_path(sec, path)

                                safe_after(root, _salvaged)
                            if recovered_n:
                                append_image_log(
                                    png_dir,
                                    f"완료 전 회수 {recovered_n}개 — "
                                    + ", ".join(
                                        f"SRT_{int(s):03d}"
                                        for s in (
                                            salvage.get("recovered_secs") or []
                                        )[:20]
                                    ),
                                )
                        except Exception as salvage_ex:
                            append_image_log(
                                png_dir,
                                f"완료 전 회수 경고: {salvage_ex}",
                            )
                    # 예외 카운트 대신 실제 파일 기준으로 실패 확정
                    fail_scenes = [
                        sc
                        for sc in todo
                        if not png_already_exists(png_dir, sc.sec)
                    ]
                    fail_labels = [sc.label for sc in fail_scenes]
                    failed_n = len(fail_scenes)
                    saved_n = sum(
                        1 for sc in todo if png_already_exists(png_dir, sc.sec)
                    )
                    if fail_labels:
                        append_image_log(
                            png_dir,
                            f"다운로드 실패 확정 {failed_n}건: "
                            + ", ".join(fail_labels[:30])
                            + (f" 외 {failed_n - 30}" if failed_n > 30 else ""),
                        )
                    else:
                        append_image_log(
                            png_dir,
                            "다운로드 실패 없음 — 대상 씬 PNG 모두 확인",
                        )

                def done() -> None:
                    set_busy(False)
                    reload_scenes()
                    left_n = (
                        len(remaining)
                        if (cancelled_wait or browser_aborted or user_cancelled)
                        else 0
                    )
                    will_shutdown = bool(shutdown_var.get()) and not script_preview
                    shutdown_note = ""
                    if will_shutdown:
                        delay_sec = _SHUTDOWN_DELAY_SEC
                        try:
                            close_chrome_debug()
                        except Exception:
                            pass
                        append_image_log(
                            png_dir,
                            f"PC 종료 예약 {delay_sec}초 후 "
                            f"(취소: shutdown /a)",
                        )
                        _schedule_pc_shutdown(delay_sec=delay_sec)
                        shutdown_note = (
                            f"\n\n약 {delay_sec}초 후 PC가 종료됩니다."
                            "\n취소: 명령 프롬프트에서 shutdown /a"
                        )
                    fail_note = ""
                    if fail_labels:
                        shown = ", ".join(fail_labels[:15])
                        extra = (
                            f" 외 {len(fail_labels) - 15}개"
                            if len(fail_labels) > 15
                            else ""
                        )
                        fail_note = f"\n다운로드 실패: {shown}{extra}"
                    recover_note = (
                        f" · 회수 {recovered_n}" if recovered_n else ""
                    )
                    abort_prefix = (
                        "브라우저 종료 — "
                        if browser_aborted
                        else (
                            "생성 중단 — "
                            if user_cancelled
                            else ("대기 취소 — " if cancelled_wait else "완료 — ")
                        )
                    )
                    set_status(
                        f"{title} "
                        + abort_prefix
                        + f"저장 {saved_n} · 건너뜀 {skipped_n}"
                        + (f" · 실패 {failed_n}" if failed_n else "")
                        + recover_note
                        + (f" · 남음 {left_n}" if left_n else "")
                        + (
                            f" · PC 종료 {_SHUTDOWN_DELAY_SEC}초 후"
                            if will_shutdown
                            else ""
                        )
                        + f" → {png_dir}"
                    )
                    msg = (
                        (
                            "브라우저 종료 — 생성 중단\n"
                            if browser_aborted
                            else (
                                "생성 중단\n"
                                if user_cancelled
                                else ("한도 대기 취소\n" if cancelled_wait else "")
                            )
                        )
                        + f"저장 {saved_n}개 · 건너뜀 {skipped_n}개"
                        + (f" · 실패 {failed_n}개" if failed_n else "")
                        + (f" · 회수 {recovered_n}개" if recovered_n else "")
                        + (f" · 남음 {left_n}개" if left_n else "")
                        + fail_note
                        + f"\n{png_dir}"
                        + shutdown_note
                    )
                    show_toast(
                        root,
                        msg,
                        title="2_5 sceneImage · 완료",
                    )

                safe_after(root, done)
            except Exception as e:
                if gen_cancel.is_set() or is_browser_closed_error(e):
                    try:
                        append_image_log(
                            png_dir,
                            f"{title} 중단 — 창 종료·취소",
                        )
                    except Exception:
                        pass

                    def cancelled() -> None:
                        set_busy(False)
                        set_status("생성 중단 — 창 종료")

                    safe_after(root, cancelled)
                    return
                err = str(e)
                try:
                    append_image_log(png_dir, f"{title} 오류: {err}")
                except Exception:
                    pass

                def fail() -> None:
                    set_busy(False)
                    set_status(f"오류: {err}")
                    safe_messagebox(root, "showerror", app_label, err)

                safe_after(root, fail)

        set_busy(True)
        set_status(
            f"{title} 준비 — {len(todo)}개"
            + (" · 입력창 준비" if need_prepare else " · 입력창 전송")
            + (" · 한도대기ON" if do_limit_wait else "")
        )
        threading.Thread(target=work, daemon=True).start()

    def add_instance() -> None:
        """다른 장(루트)용 sceneImage 창을 병렬 실행."""
        try:
            before = count_claimable_slots()
            _spawn_scene_image_instance(script_preview=script_preview)
        except Exception as e:
            safe_messagebox(root, "showerror", app_label, str(e))
            return
        slot = get_active_slot()
        slot_lbl = slot.label if slot else "?"
        left = max(0, before - 1)
        set_status(
            f"새 인스턴스 실행 — 이 창 [{slot_lbl}] · "
            f"남은 슬롯 {left}개 · 장(루트)만 다르게 지정"
        )

    def open_browser() -> None:
        """재오픈 → SRT·프롬프트·명령 → 생성·다운로드·요청대기 반복까지 일괄."""
        if busy["v"]:
            safe_messagebox(
                root,
                "showinfo",
                "2_5 sceneImage",
                "이미 작업 중입니다. 끝난 뒤 다시 누르세요.",
            )
            return
        browser_ready["v"] = False
        input_prepared["v"] = False
        input_prepared["cmd_sec"] = None
        try:
            apply_root(force=False, sync_single_fields=False)
        except Exception:
            reload_scenes(sync_single_fields=False)
        png_dir = Path(png_var.get().strip() or ".")
        if not str(png_dir).strip() or png_dir == Path("."):
            safe_messagebox(root, "showwarning", "2_5 sceneImage", "png 폴더를 지정하세요.")
            return
        srt_now = srt_var.get().strip()
        if not srt_now or not Path(srt_now).is_file():
            found = find_default_srt(root_var.get().strip() or ".")
            if found is not None:
                srt_var.set(str(found))
            else:
                safe_messagebox(
                    root,
                    "showwarning",
                    "2_5 sceneImage",
                    "SRT 파일이 없습니다.\n"
                    "루트 하위 mp3/new.srt 또는 all.srt 를 지정하세요.",
                )
                return
        todo = [sc for sc in scenes if not png_already_exists(png_dir, sc.sec)]
        sel = _parse_manual_secs(manual_var.get(), [sc.sec for sc in scenes])
        if sel:
            sel_set = set(sel)
            todo = [sc for sc in todo if sc.sec in sel_set]
            if not todo:
                safe_messagebox(
                    root,
                    "showinfo",
                    "2_5 sceneImage",
                    "구간 내 생성할 씬이 없거나 PNG가 이미 있습니다.\n"
                    f"구간: {manual_var.get().strip()}",
                )
                return
        _run_scenes(
            todo,
            open_browser_first=True,
            title="실행·생성",
            force_reopen=True,
        )

    def manual_generate() -> None:
        if busy["v"]:
            return
        reload_scenes()
        avail = [sc.sec for sc in scenes]
        secs = _parse_manual_secs(manual_var.get(), avail)
        if not secs:
            safe_messagebox(
                root,
                "showwarning",
                "2_5 sceneImage",
                "초·구간을 입력하세요.\n예: 10,20,120 · 220~500 · 720~",
            )
            return
        by_sec = {sc.sec: sc for sc in scenes}
        todo: list[SceneLine] = []
        missing: list[int] = []
        for sec in secs:
            sc = by_sec.get(sec)
            if sc is None:
                missing.append(sec)
            else:
                todo.append(sc)
        if missing:
            safe_messagebox(
                root,
                "showwarning",
                "2_5 sceneImage",
                "씬 목록에 없는 초: "
                + ", ".join(str(s) for s in missing)
                + "\n이미지프롬프트·SRT 간격을 확인하세요.",
            )
            if not todo:
                return
        persist()
        _run_scenes(todo, open_browser_first=False, title="수동 생성")

    def single_generate() -> None:
        if busy["v"]:
            return
        sec = _parse_single_sec(single_sec_var.get())
        if sec is None:
            sc_sel = selected_scene()
            if sc_sel is not None:
                sec = int(sc_sel.sec)
        if sec is None:
            safe_messagebox(
                root,
                "showwarning",
                app_label,
                "이미지 번호를 입력하세요.\n"
                "예: 120 · SRT_120 · 또는 왼쪽 목록에서 SRT# 선택",
            )
            return
        single_sec_var.set(str(sec))
        prompt = _read_single_prompt()
        by_sec = {sc.sec: sc for sc in scenes}
        found = by_sec.get(sec)
        script_only = False
        if script_preview and not is_real_scene_prompt(prompt):
            if found and is_real_scene_prompt(found.prompt):
                prompt = found.prompt.strip()
                _set_single_prompt(prompt)
            else:
                script_only = True
                prompt = (found.prompt.strip() if found and found.prompt.strip() else "")
        elif not prompt:
            if found and found.prompt.strip():
                prompt = found.prompt.strip()
                _set_single_prompt(prompt)
            else:
                safe_messagebox(
                    root,
                    "showwarning",
                    app_label,
                    "생성할 내용을 입력하세요.\n"
                    "씬 목록에서 선택하면 프롬프트가 채워집니다.",
                )
                return
        if script_only:
            scene_sec_list = _script_scene_secs()
            slot = previous_reference_slot_sec(
                sec,
                interval_sec=_SCENE_INTERVAL_SEC,
                scene_secs=scene_sec_list,
            )
            png_dir = Path(png_var.get().strip() or ".")
            ref_path = find_previous_reference_png(
                png_dir,
                sec,
                interval_sec=_SCENE_INTERVAL_SEC,
                scene_secs=scene_sec_list,
            )
            if slot is None:
                safe_messagebox(
                    root,
                    "showwarning",
                    app_label,
                    f"SRT_{sec:03d} — 직전 참조 씬이 없습니다.",
                )
                return
            if ref_path is None:
                safe_messagebox(
                    root,
                    "showwarning",
                    app_label,
                    f"SRT_{sec:03d} — 직전 씬 이미지({srt_png_name(slot)} 등)가 없습니다.\n"
                    "먼저 직전 구간 이미지를 생성하세요.",
                )
                return
        persist()
        update_single_ref_hint()
        ref_note = single_ref_var.get()
        use_ref = True if script_only else bool(prev_ref_var.get())
        append_image_log(
            Path(png_var.get().strip() or "."),
            f"개별 생성 요청 SRT_{sec:03d} · "
            f"ref={'ON' if use_ref else 'OFF'}"
            + (" · 대본구간" if script_only else "")
            + (
                " · 브라우저 준비됨(명령만)"
                if script_preview and browser_ready["v"]
                else (
                    " · 브라우저 최초 준비(로그인 — Chrome에서 Google 확인)"
                    if script_preview
                    else ""
                )
            )
            + f" · {ref_note}",
        )
        sc = SceneLine(sec=int(sec), prompt=prompt)
        _run_scenes(
            [sc],
            open_browser_first=not browser_ready["v"],
            title="개별 생성",
            force_regenerate=True,
            force_reopen=False,
            attach_reference_override=True if script_only else None,
            dialogue_until_next_scene=script_only,
            script_light_run=script_preview,
        )

    btn_browser = ttk.Button(act, text="실행", command=open_browser)
    btn_browser.pack(side=tk.LEFT, padx=(0, 6))
    btn_refresh = ttk.Button(act, text="새로고침", width=8, command=refresh_chapter_settings)
    btn_refresh.pack(side=tk.LEFT, padx=(0, 6))
    if not script_preview:
        ttk.Button(act, text="인스턴스추가", command=add_instance).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        btn_cancel_wait = ttk.Button(
            act,
            text="대기 취소",
            width=10,
            command=cancel_limit_wait,
            state=tk.DISABLED,
        )
        btn_cancel_wait.pack(side=tk.LEFT, padx=(0, 6))
        ttk.Checkbutton(
            act,
            text="완료후 PC종료",
            variable=shutdown_var,
            command=persist,
        ).pack(side=tk.LEFT, padx=(8, 0))

    ttk.Label(frm, textvariable=scene_var).grid(row=2, column=0, sticky="w", pady=(0, 4))

    # --- main panes ---
    paned = ttk.Panedwindow(frm, orient=tk.VERTICAL)
    paned.grid(row=3, column=0, sticky="nsew")

    manual_fr = ttk.LabelFrame(
        paned, text="이미지 구간 생성 (10,20 / 220~500 / 720~ …)", padding=4
    )
    single_fr = ttk.LabelFrame(
        paned, text="개별 이미지 생성 (번호 + 내용)", padding=4
    )
    lists_fr = ttk.Frame(paned)
    paned.add(manual_fr, weight=1)
    paned.add(single_fr, weight=2)
    paned.add(lists_fr, weight=3)

    manual_fr.grid_columnconfigure(0, weight=1)
    ttk.Label(
        manual_fr,
        text="특정 초·구간만 생성 — 비우면 「실행」은 전체 미생성 씬.",
        foreground="#555",
    ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))
    manual_ent = ttk.Entry(manual_fr, textvariable=manual_var)
    manual_ent.grid(row=1, column=0, sticky="ew", padx=(0, 6))
    btn_manual = ttk.Button(manual_fr, text="선택 생성", width=10, command=manual_generate)
    btn_manual.grid(row=1, column=1, sticky="e")

    single_fr.grid_columnconfigure(1, weight=1)
    single_fr.grid_rowconfigure(2, weight=1)
    ttk.Label(single_fr, text="번호(초)", width=10).grid(
        row=0, column=0, sticky="nw", pady=(0, 4)
    )
    single_num_row = ttk.Frame(single_fr)
    single_num_row.grid(row=0, column=1, sticky="ew", pady=(0, 4))
    single_num_row.grid_columnconfigure(0, weight=1)
    single_sec_ent = ttk.Entry(single_num_row, textvariable=single_sec_var, width=10)
    single_sec_ent.grid(row=0, column=0, sticky="w")
    ttk.Label(single_num_row, textvariable=single_ref_var, foreground="#555").grid(
        row=0, column=1, sticky="w", padx=(10, 0)
    )
    ttk.Label(
        single_fr,
        text=(
            "내용 (비우면 대본 구간으로 생성)"
            if script_preview
            else "내용 (SCENE PROMPT — 비우면 씬 목록 프롬프트 사용)"
        ),
        foreground="#555",
    ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 4))
    single_prompt_wrap = ttk.Frame(single_fr)
    single_prompt_wrap.grid(row=2, column=0, columnspan=2, sticky="nsew", pady=(0, 4))
    single_prompt_wrap.grid_columnconfigure(0, weight=1)
    single_prompt_wrap.grid_rowconfigure(0, weight=1)
    single_prompt_text = tk.Text(
        single_prompt_wrap,
        height=5,
        wrap=tk.WORD,
        undo=True,
    )
    single_prompt_sb = ttk.Scrollbar(
        single_prompt_wrap, orient=tk.VERTICAL, command=single_prompt_text.yview
    )
    single_prompt_text.configure(yscrollcommand=single_prompt_sb.set)
    single_prompt_text.grid(row=0, column=0, sticky="nsew")
    single_prompt_sb.grid(row=0, column=1, sticky="ns")
    single_prompt_box["w"] = single_prompt_text
    if single_prompt_default:
        _set_single_prompt(single_prompt_default)
    btn_single = ttk.Button(
        single_fr, text="개별 이미지 생성", width=16, command=single_generate
    )
    btn_single.grid(row=3, column=1, sticky="e", pady=(2, 0))
    single_sec_var.trace_add("write", update_single_ref_hint)
    png_var.trace_add("write", update_single_ref_hint)
    prev_ref_var.trace_add("write", update_single_ref_hint)

    lists_fr.grid_columnconfigure(0, weight=1)
    lists_fr.grid_rowconfigure(0, weight=1)

    link_list: tk.Listbox | None = None

    preview_image_scale_var = tk.IntVar(value=preview_scale_default)
    preview_scale_show_var = tk.StringVar(value=f"{preview_scale_default}px")

    if script_preview:
        h_paned = ttk.Panedwindow(lists_fr, orient=tk.HORIZONTAL)
        h_paned.grid(row=0, column=0, sticky="nsew")
        list_frm = ttk.LabelFrame(h_paned, text="씬 · 대본 · PNG", padding=4)
        preview_frm = ttk.Frame(h_paned, padding=8)
        h_paned.add(list_frm, weight=2)
        h_paned.add(preview_frm, weight=4)
        list_frm.grid_rowconfigure(0, weight=1)
        list_frm.grid_columnconfigure(0, weight=1)
        preview_frm.grid_columnconfigure(0, weight=1)
        preview_frm.grid_rowconfigure(0, weight=1)

        cols = ("sec", "time", "reason", "cue", "png")
        scene_tree = ttk.Treeview(list_frm, columns=cols, show="headings", height=12)
        scene_tree.heading("sec", text="SRT#")
        scene_tree.heading("time", text="시간")
        scene_tree.heading("reason", text="이유")
        scene_tree.heading("cue", text="대본(마우스올리기)")
        scene_tree.heading("png", text="PNG")
        scene_tree.column("sec", width=64, anchor=tk.CENTER, stretch=False)
        scene_tree.column("time", width=52, anchor=tk.CENTER, stretch=False)
        scene_tree.column("reason", width=48, anchor=tk.CENTER, stretch=False)
        scene_tree.column("cue", width=280, anchor=tk.W, stretch=True)
        scene_tree.column("png", width=44, anchor=tk.CENTER, stretch=False)
        tree_vsb = ttk.Scrollbar(list_frm, orient=tk.VERTICAL, command=scene_tree.yview)
        tree_hsb = ttk.Scrollbar(list_frm, orient=tk.HORIZONTAL, command=scene_tree.xview)
        scene_tree.configure(yscrollcommand=tree_vsb.set, xscrollcommand=tree_hsb.set)
        scene_tree.grid(row=0, column=0, sticky="nsew")
        tree_vsb.grid(row=0, column=1, sticky="ns")
        tree_hsb.grid(row=1, column=0, sticky="ew")

        img_frm = ttk.Frame(preview_frm)
        img_frm.grid(row=0, column=0, sticky="nsew")
        img_frm.grid_columnconfigure(0, weight=1)
        img_frm.grid_rowconfigure(1, weight=1)

        img_hdr = ttk.Frame(img_frm)
        img_hdr.grid(row=0, column=0, sticky="ew")
        img_hdr.grid_columnconfigure(1, weight=1)
        ttk.Label(
            img_hdr,
            text="SRT_XXX.png — SRT# 클릭 선택 · 더블클릭 확대 · 행에 마우스=대본",
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(img_hdr, textvariable=preview_scale_show_var).grid(
            row=0, column=2, sticky="e", padx=(8, 0)
        )
        preview_img_wrap = tk.Frame(img_frm, bg="#e8e8e8")
        preview_img_wrap.grid(row=1, column=0, sticky="nsew", pady=(4, 4))
        preview_img_wrap.grid_columnconfigure(0, weight=1)
        preview_img_wrap.grid_rowconfigure(0, weight=1)
        preview_image_lbl = tk.Label(
            preview_img_wrap,
            text="(SRT# 클릭 — 이미지 없음)",
            anchor=tk.CENTER,
            bg="#e8e8e8",
        )
        preview_image_lbl.grid(row=0, column=0, sticky="nsew")
        scale_fr = ttk.Frame(img_frm)
        scale_fr.grid(row=2, column=0, sticky="ew")
        ttk.Label(scale_fr, text="이미지 크기").pack(side=tk.LEFT)
        preview_scale = ttk.Scale(
            scale_fr,
            from_=160,
            to=960,
            orient=tk.HORIZONTAL,
        )
        preview_scale.set(preview_scale_default)
        preview_scale.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 0))
    else:
        lists_fr.grid_columnconfigure(1, weight=1)
        left = ttk.LabelFrame(lists_fr, text="파싱된 씬", padding=4)
        right = ttk.LabelFrame(lists_fr, text="저장된 경로", padding=4)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        right.grid(row=0, column=1, sticky="nsew")
        left.grid_columnconfigure(0, weight=1)
        left.grid_rowconfigure(0, weight=1)
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(0, weight=1)

        scene_list = tk.Listbox(left, activestyle="dotbox", exportselection=False)
        scene_sb = ttk.Scrollbar(left, orient=tk.VERTICAL, command=scene_list.yview)
        scene_list.configure(yscrollcommand=scene_sb.set)
        scene_list.grid(row=0, column=0, sticky="nsew")
        scene_sb.grid(row=0, column=1, sticky="ns")

        link_list = tk.Listbox(right, activestyle="dotbox", exportselection=False)
        link_sb = ttk.Scrollbar(right, orient=tk.VERTICAL, command=link_list.yview)
        link_list.configure(yscrollcommand=link_sb.set)
        link_list.grid(row=0, column=0, sticky="nsew")
        link_sb.grid(row=0, column=1, sticky="ns")

    def _scene_cue_tooltip_text(sc: SceneLine) -> str:
        nxt = _next_scene_sec(sc.sec)
        header = f"{sc.label} ({_format_scene_time(sc.sec)}"
        if nxt is not None:
            header += f" ~ {_format_scene_time(nxt)} 직전"
        else:
            header += " ~ 끝"
        header += ")"
        if sc.cut_kind == "transition" and sc.cut_reason:
            header += f"\n추가컷: {sc.cut_reason}"
        header += "\n\n"
        cue = _scene_cue_text(sc)
        return header + (cue or "(해당 구간 대본 없음)")

    def _hide_tree_cue_tooltip() -> None:
        after_id = tree_cue_tooltip.get("after")
        if after_id is not None:
            try:
                root.after_cancel(after_id)  # type: ignore[arg-type]
            except tk.TclError:
                pass
            tree_cue_tooltip["after"] = None
        win = tree_cue_tooltip.get("win")
        if win is not None:
            try:
                win.destroy()  # type: ignore[union-attr]
            except tk.TclError:
                pass
            tree_cue_tooltip["win"] = None
        tree_cue_tooltip["iid"] = None

    def _show_tree_cue_tooltip(iid: str, x_root: int, y_root: int) -> None:
        _hide_tree_cue_tooltip()
        try:
            idx = int(iid)
        except ValueError:
            return
        if idx < 0 or idx >= len(scenes):
            return
        text = _scene_cue_tooltip_text(scenes[idx])
        if not text.strip():
            return
        win = tk.Toplevel(root)
        win.wm_overrideredirect(True)
        try:
            win.wm_attributes("-topmost", True)
        except tk.TclError:
            pass
        border = tk.Frame(
            win,
            background="#c8c8c8",
            borderwidth=1,
            relief=tk.SOLID,
        )
        border.pack()
        lbl = tk.Label(
            border,
            text=text,
            justify=tk.LEFT,
            background="#ffffe1",
            foreground="#111",
            font=(fam, max(9, sz - 1)),
            wraplength=440,
            padx=10,
            pady=8,
        )
        lbl.pack(padx=1, pady=1)
        tree_cue_tooltip["win"] = win
        tree_cue_tooltip["iid"] = iid
        win.update_idletasks()
        sw = max(120, win.winfo_width())
        sh = max(40, win.winfo_height())
        sx = root.winfo_screenwidth()
        sy = root.winfo_screenheight()
        px = min(max(4, x_root + 14), max(4, sx - sw - 8))
        py = min(max(4, y_root + 18), max(4, sy - sh - 8))
        win.geometry(f"+{px}+{py}")

    def _on_tree_motion(event: tk.Event) -> None:
        if scene_tree is None:
            return
        if scene_tree.identify_region(event.x, event.y) != "cell":
            _hide_tree_cue_tooltip()
            return
        iid = scene_tree.identify_row(event.y)
        if not iid:
            _hide_tree_cue_tooltip()
            return
        if iid == tree_cue_tooltip.get("iid") and tree_cue_tooltip.get("win"):
            return
        _hide_tree_cue_tooltip()

        def _show_delayed() -> None:
            tree_cue_tooltip["after"] = None
            _show_tree_cue_tooltip(iid, event.x_root, event.y_root)

        tree_cue_tooltip["after"] = root.after(280, _show_delayed)

    def _preview_image_max_px() -> int:
        """슬라이더 값 = PNG 미리보기 최대 변(가로·세로) px."""
        try:
            if preview_scale is not None:
                px = int(float(preview_scale.get()))
            else:
                px = int(preview_image_scale_var.get())
        except (tk.TclError, ValueError):
            px = preview_scale_default
        return max(160, min(960, px))

    def _on_preview_scale_change(*_a: object) -> None:
        if not script_preview:
            return
        px = _preview_image_max_px()
        preview_image_scale_var.set(px)
        preview_scale_show_var.set(f"{px}px")
        sc = selected_scene()
        if sc is not None:
            _show_scene_png_preview(sc, force_reload=True)
        persist()

    if script_preview and preview_scale is not None:
        preview_scale.configure(command=_on_preview_scale_change)

    def _apply_preview_photo(photo: object) -> None:
        if preview_image_lbl is None:
            return
        preview_thumb_refs.append(photo)
        preview_image_lbl.configure(image=photo, text="", bg="#e8e8e8")
        preview_image_lbl.image = photo

    def _show_scene_png_preview(
        sc: SceneLine | None, *, force_reload: bool = False
    ) -> None:
        if not script_preview or preview_image_lbl is None:
            return
        if sc is None:
            preview_image_lbl.configure(
                image="",
                text="(SRT# 클릭)",
                bg="#e8e8e8",
            )
            preview_image_lbl.image = None
            preview_png_path["v"] = None
            return
        png_path = scene_png_path(Path(png_var.get().strip() or "."), sc.sec)
        try:
            png_resolved = png_path.resolve() if png_path.is_file() else png_path
        except OSError:
            png_resolved = png_path
        if not png_resolved.is_file():
            preview_image_lbl.configure(
                image="",
                text=f"{sc.png_name} — 없음",
                bg="#e8e8e8",
            )
            preview_image_lbl.image = None
            preview_png_path["v"] = None
            return
        max_px = _preview_image_max_px()
        cache_key = f"{png_resolved}|{max_px}"
        try:
            st = png_resolved.stat()
            stamp = f"{st.st_mtime_ns}|{st.st_size}"
        except OSError:
            stamp = ""
        preview_png_path["v"] = png_resolved
        cached = preview_photo_cache.get(cache_key)
        if force_reload or (cached is not None and cached[0] != stamp):
            preview_photo_cache.pop(cache_key, None)
        elif cached is not None:
            _apply_preview_photo(cached[1])
            return

        preview_load_token["n"] += 1
        load_id = int(preview_load_token["n"])
        preview_image_lbl.configure(
            image="",
            text="불러오는 중…",
            bg="#e8e8e8",
        )

        def work() -> None:
            err = ""
            photo = None
            try:
                from PIL import Image, ImageTk

                im = Image.open(png_resolved).convert("RGB")
                im.thumbnail((max_px, max_px))
                photo = ImageTk.PhotoImage(im)
            except Exception as e:
                err = str(e)
                try:
                    photo = tk.PhotoImage(file=str(png_resolved))
                except Exception as e2:
                    err = f"{err}; {e2}"

            def ui() -> None:
                if load_id != preview_load_token["n"]:
                    return
                if preview_png_path["v"] != png_resolved:
                    return
                if photo is None:
                    preview_image_lbl.configure(
                        image="",
                        text=f"미리보기 실패\n{err[:120]}",
                        bg="#e8e8e8",
                    )
                    preview_image_lbl.image = None
                    return
                preview_photo_cache[cache_key] = (stamp, photo)
                _apply_preview_photo(photo)

            safe_after(root, ui)

        threading.Thread(target=work, daemon=True).start()

    def _open_png_viewer(path: Path) -> None:
        try:
            from PIL import Image, ImageTk

            im = Image.open(path).convert("RGB")
            im.thumbnail((960, 720))
            photo = ImageTk.PhotoImage(im)
            preview_thumb_refs.append(photo)
        except Exception as e:
            safe_messagebox(root, "showerror", app_label, f"이미지를 열 수 없습니다.\n{e}")
            return
        win = tk.Toplevel(root)
        win.title(path.name)
        lbl = tk.Label(win, image=photo)
        lbl.image = photo
        lbl.pack(padx=10, pady=10)
        ttk.Label(win, text=str(path), foreground="#666").pack(pady=(0, 8))

    def on_scene_select(_event: tk.Event | None = None) -> None:
        sc = selected_scene()
        if sc is None:
            scene_var.set("")
            _show_scene_png_preview(None)
            return
        exists = png_already_exists(Path(png_var.get().strip() or "."), sc.sec)
        mark = " · 이미 있음" if exists else ""
        scene_var.set(f"{sc.label} → {sc.png_name}  |  {len(sc.prompt)}자{mark}")
        single_sec_var.set(str(sc.sec))
        _set_single_prompt(sc.prompt)
        update_single_ref_hint()
        _show_scene_png_preview(sc)
        persist()

    if script_preview and scene_tree is not None:

        def _on_tree_click(event: tk.Event) -> None:
            if scene_tree.identify_region(event.x, event.y) != "cell":
                return
            iid = scene_tree.identify_row(event.y)
            if not iid:
                return
            scene_tree.selection_set(iid)
            scene_tree.focus(iid)
            on_scene_select()

        scene_tree.bind("<ButtonRelease-1>", _on_tree_click)
        scene_tree.bind("<<TreeviewSelect>>", on_scene_select)
        scene_tree.bind("<KeyRelease-Up>", on_scene_select)
        scene_tree.bind("<KeyRelease-Down>", on_scene_select)
        scene_tree.bind("<Motion>", _on_tree_motion)
        scene_tree.bind("<Leave>", lambda _e: _hide_tree_cue_tooltip())

        def _on_tree_dbl(_e: tk.Event) -> None:
            sc = selected_scene()
            if sc is None:
                return
            p = scene_png_path(Path(png_var.get().strip() or "."), sc.sec)
            if p.is_file():
                _open_png_viewer(p)

        scene_tree.bind("<Double-1>", _on_tree_dbl)
    elif scene_list is not None:
        scene_list.bind("<<ListboxSelect>>", on_scene_select)

    ttk.Label(frm, textvariable=status_var).grid(row=4, column=0, sticky="ew", pady=(8, 0))
    if script_preview:
        tip = (
            "「실행」= 재오픈 → 생성·다운로드 반복"
            " · 「새로고침」= 장(루트) png·SRT·프롬프트·씬·characters.json 재로드"
            " · 행 마우스=대본 풍선 · SRT# 클릭=PNG · 크기 슬라이더 · 더블클릭 확대"
            " · 개별 생성: 번호+내용(비우면 대본) · 직전 씬 참조 · 기존 PNG 덮어쓰기"
            " · 실패 로그: image_fail.log"
        )
    else:
        tip = (
            "「실행」= 재오픈 → 생성·다운로드 반복"
            " · 「새로고침」= 장(루트) png·SRT·프롬프트·씬·characters.json 재로드"
            " · 「인스턴스추가」= 다른 장(루트) 병렬 다운로드 · 슬롯(포트) 자동 분리"
            " · 한도 시: 브라우저 종료 → 정상화 시각 확인(배너·필요 시 재오픈) → 대기 → 재개"
            " · 개별 생성: 번호+내용 → t-20 참조 첨부(체크)·기존 PNG 덮어쓰기"
            " · 완료후 PC종료: 체크 시 실행 종료 후 약 60초 뒤 · 취소 shutdown /a"
            " · 실패 로그: image_fail.log"
        )
    ttk.Label(frm, text=tip, foreground="#555").grid(row=5, column=0, sticky="w", pady=(4, 0))

    def on_close() -> None:
        if script_preview:
            _hide_tree_cue_tooltip()
        cancel_generation(reason="창 종료")
        persist()
        release_chrome_slot()

    if standalone:
        bind_close(root, standalone, on_close)
    else:
        bind_hub_destroy(root, on_close)

    def _boot() -> None:
        saved_sec = single_sec_var.get().strip()
        saved_prompt = _read_single_prompt()
        saved_manual = manual_var.get().strip()
        if root_var.get().strip():
            apply_root(force=False, sync_single_fields=False)
        elif png_var.get().strip():
            auto_assign_from_png(force=False)
        else:
            reload_scenes(sync_single_fields=False)
        if saved_sec:
            single_sec_var.set(saved_sec)
        if saved_prompt:
            _set_single_prompt(saved_prompt)
        if saved_manual:
            manual_var.set(saved_manual)
        update_single_ref_hint()
        persist()
        if script_preview:
            sc = selected_scene()
            if sc is not None:
                _show_scene_png_preview(sc, force_reload=True)

    root.after(150, _boot)
    run_mainloop(root, standalone)

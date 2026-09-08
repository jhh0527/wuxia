# -*- coding: utf-8 -*-
"""1_3_plotToPrompt GUI — 줄거리→BRIEF · BRIEF→합본·tts."""

from __future__ import annotations

import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, font as tkfont, messagebox, scrolledtext, ttk

from plot_to_prompt import __version__
from plot_to_prompt import diag_log
from plot_to_prompt.bible_lookup import load_chapter_meta, meta_status_line
from plot_to_prompt.bible_sync import sync_plot_to_bible
from plot_to_prompt.brief_builder import BriefInput, build_brief_markdown
from plot_to_prompt.credentials import load_credentials, save_credentials
from plot_to_prompt.paths import (
    GENSPARK_AI_CHAT_URL,
    brief_path,
    default_log_path,
    default_novel_root,
    default_output_dir,
    default_work_root,
    genspark_profile_dir,
    infer_novel_root_from_work,
    parse_chapter_from_path,
)
from plot_to_prompt.settings import load_gui_settings, save_gui_settings
from plot_to_prompt.tts_packet import (
    CHAPTER_END,
    CHAPTER_START,
    body_from_export,
    build_packet_from_brief,
    chapter_tts_path,
    extract_chapter_body,
    looks_like_genspark_packet,
    read_export_text,
    resolve_brief_file,
    resolve_clipboard_body,
    save_chapter_to_tts,
)


def _default_font() -> tuple[str, int]:
    try:
        f = tkfont.nametofont("TkDefaultFont")
        return (f.actual("family"), max(10, int(f.actual("size"))))
    except tk.TclError:
        return ("맑은 고딕", 10)


def _clipboard_set(root: tk.Misc, text: str) -> None:
    root.clipboard_clear()
    root.clipboard_append(text)
    root.update_idletasks()


def _open_genspark() -> bool:
    """기본 브라우저로 젠스파크 AI 채팅 열기."""
    import os
    import sys

    try:
        if sys.platform.startswith("win"):
            os.startfile(GENSPARK_AI_CHAT_URL)  # type: ignore[attr-defined]
            diag_log.log(f"젠스파크 열기(startfile) url={GENSPARK_AI_CHAT_URL}")
            return True
        ok = webbrowser.open(GENSPARK_AI_CHAT_URL, new=2)
        diag_log.log(f"젠스파크 열기 ok={ok} url={GENSPARK_AI_CHAT_URL}")
        return bool(ok)
    except Exception as ex:
        diag_log.log(f"젠스파크 열기 실패: {ex!r}")
        try:
            ok = webbrowser.open(GENSPARK_AI_CHAT_URL, new=2)
            return bool(ok)
        except Exception as ex2:
            diag_log.log(f"젠스파크 webbrowser 실패: {ex2!r}")
            return False


def main(*, container: tk.Misc | None = None) -> None:
    from wisdom_gui_host import apply_window_chrome, bind_close, run_mainloop, tk_host
    from wisdom_workspace import folder_dialog_initial, touch_workspace_from_path

    cfg = load_gui_settings()
    novel_default = Path(cfg["novel_root"]) if cfg.get("novel_root") else default_novel_root()
    if not novel_default.is_dir():
        novel_default = default_novel_root()
    work_default = Path(cfg["work_root"]) if cfg.get("work_root") else default_work_root()
    ch_from_work = parse_chapter_from_path(work_default)
    # 작업 루트(…/N부/M장)에서 작품 폴더를 찾을 수 있으면 우선
    inferred_novel = infer_novel_root_from_work(work_default)
    if inferred_novel is not None and (
        not novel_default.is_dir()
        or not (novel_default / "chapter_map.md").is_file()
    ):
        novel_default = inferred_novel


    root, standalone = tk_host(container)
    apply_window_chrome(
        root,
        standalone,
        title=f"1_3_plotToPrompt {__version__}",
        minsize=(720, 620),
        geometry="880x720",
    )
    fam, sz = _default_font()
    root.option_add("*Font", (fam, sz))

    log_file = diag_log.start_session(
        default_log_path(),
        title=f"1_3_plotToPrompt {__version__}",
    )
    diag_log.log(f"시작 novel={novel_default} work={work_default}")

    novel_var = tk.StringVar(value=str(novel_default))
    work_var = tk.StringVar(value=str(work_default))
    chapter_disp_var = tk.StringVar(
        value=f"{ch_from_work}장" if ch_from_work else "(경로에 N장 없음)"
    )
    meta_var = tk.StringVar(value="")
    status_var = tk.StringVar(
        value="작업 루트(…/N장)에서 장 자동. 줄거리(~700자). 로그는 하단."
    )
    progress_var = tk.StringVar(value="대기")
    plot_count_var = tk.StringVar(value="0자")
    body_count_var = tk.StringVar(value="0자")
    sync_bible_default = (cfg.get("sync_bible") or "1").strip() in (
        "1",
        "true",
        "True",
        "yes",
        "on",
    )
    sync_bible_var = tk.BooleanVar(value=sync_bible_default)
    cred_email, cred_pw = load_credentials()
    email_var = tk.StringVar(value=cred_email)
    pw_var = tk.StringVar(value=cred_pw)
    run_busy = {"on": False}

    outer = ttk.Frame(root, padding=10)
    outer.pack(fill=tk.BOTH, expand=True)

    # --- progress + status ---
    prog_fr = ttk.Frame(outer)
    prog_fr.pack(fill=tk.X, side=tk.BOTTOM, pady=(6, 0))
    ttk.Label(prog_fr, text="진척").pack(side=tk.LEFT)
    progress = ttk.Progressbar(prog_fr, mode="determinate", maximum=100, length=180)
    progress.pack(side=tk.LEFT, padx=6)
    ttk.Label(prog_fr, textvariable=progress_var, width=28).pack(side=tk.LEFT)
    ttk.Label(outer, textvariable=status_var, wraplength=840).pack(
        fill=tk.X, side=tk.BOTTOM, pady=(2, 0)
    )

    log_fr = ttk.LabelFrame(outer, text=f"로그 ({log_file})", padding=4)
    log_fr.pack(fill=tk.BOTH, side=tk.BOTTOM, pady=(6, 0))
    log_text = scrolledtext.ScrolledText(log_fr, height=6, wrap=tk.WORD, state=tk.DISABLED)
    log_text.pack(fill=tk.BOTH, expand=True)

    def _append_log_line(line: str) -> None:
        try:
            log_text.configure(state=tk.NORMAL)
            log_text.insert(tk.END, line + "\n")
            log_text.see(tk.END)
            log_text.configure(state=tk.DISABLED)
        except tk.TclError:
            pass

    diag_log.add_listener(_append_log_line)

    def _set_progress(step: int, total: int, msg: str) -> None:
        total = max(1, total)
        step = max(0, min(step, total))
        pct = int(100 * step / total)
        progress["value"] = pct
        progress_var.set(f"{pct}% ({step}/{total}) {msg}")
        try:
            root.update_idletasks()
        except tk.TclError:
            pass

    def _busy(msg: str) -> None:
        progress.configure(mode="indeterminate")
        progress.start(12)
        progress_var.set(msg)
        try:
            root.update_idletasks()
        except tk.TclError:
            pass

    def _idle(msg: str = "완료") -> None:
        try:
            progress.stop()
        except tk.TclError:
            pass
        progress.configure(mode="determinate")
        progress["value"] = 100
        progress_var.set(msg)

    path_fr = ttk.LabelFrame(outer, text="경로", padding=6)
    path_fr.pack(fill=tk.X, pady=(0, 8))

    def pick_novel() -> None:
        init = folder_dialog_initial(
            Path(novel_var.get()) if Path(novel_var.get()).is_dir() else default_novel_root()
        )
        p = filedialog.askdirectory(title="작품 폴더", initialdir=init)
        if p:
            touch_workspace_from_path(p)
            novel_var.set(p)
            diag_log.log(f"작품 폴더={p}")
            refresh_meta()

    def pick_work() -> None:
        init = folder_dialog_initial(
            Path(work_var.get()) if Path(work_var.get()).is_dir() else default_work_root()
        )
        p = filedialog.askdirectory(title="작업 루트 (…/N장 또는 tts 상위)", initialdir=init)
        if p:
            touch_workspace_from_path(p)
            work_var.set(p)
            ch = parse_chapter_from_path(p)
            if ch is not None:
                diag_log.log(f"작업 루트={p} → 장={ch}")
            else:
                diag_log.log(f"작업 루트={p} (경로에 N장 없음)")
            novel_guess = infer_novel_root_from_work(p)
            if novel_guess is not None:
                novel_var.set(str(novel_guess))
                diag_log.log(f"작품 폴더 추정={novel_guess}")
            _persist()

    r1 = ttk.Frame(path_fr)
    r1.pack(fill=tk.X, pady=2)
    ttk.Label(r1, text="작품 폴더", width=10).pack(side=tk.LEFT)
    ttk.Entry(r1, textvariable=novel_var).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)
    ttk.Button(r1, text="…", width=3, command=pick_novel).pack(side=tk.LEFT)

    r2 = ttk.Frame(path_fr)
    r2.pack(fill=tk.X, pady=2)
    ttk.Label(r2, text="작업 루트", width=10).pack(side=tk.LEFT)
    ttk.Entry(r2, textvariable=work_var).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)
    ttk.Button(r2, text="…", width=3, command=pick_work).pack(side=tk.LEFT)
    ttk.Label(r2, text="→ …/N장 필수", foreground="#555").pack(side=tk.LEFT, padx=(8, 0))

    r3 = ttk.Frame(path_fr)
    r3.pack(fill=tk.X, pady=2)
    ttk.Label(r3, text="장", width=10).pack(side=tk.LEFT)
    ttk.Label(r3, textvariable=chapter_disp_var).pack(side=tk.LEFT, padx=4)
    ttk.Label(r3, textvariable=meta_var, wraplength=520).pack(side=tk.LEFT, padx=(12, 0))

    def _persist() -> None:
        ch = parse_chapter_from_path(work_var.get().strip())
        save_gui_settings(
            novel_root=novel_var.get().strip(),
            last_chapter=str(ch) if ch else "",
            work_root=work_var.get().strip(),
            sync_bible=sync_bible_var.get(),
        )

    def _chapter_int() -> int:
        ch = parse_chapter_from_path(work_var.get().strip())
        if ch is None or ch < 1:
            raise ValueError(
                "작업 루트 경로에 N장 폴더가 필요합니다 (예: …/2부/14장)"
            )
        return ch

    def refresh_meta(*_a: object) -> None:
        ch = parse_chapter_from_path(work_var.get().strip())
        if ch is not None and ch >= 1:
            chapter_disp_var.set(f"{ch}장")
        else:
            chapter_disp_var.set("(경로에 N장 없음)")
            meta_var.set("")
            return
        novel = novel_var.get().strip()
        if not novel or not Path(novel).is_dir():
            meta_var.set("")
            return
        meta = load_chapter_meta(novel, ch)
        line = meta_status_line(meta)
        meta_var.set("메타: " + line)
        diag_log.log(f"메타 ch={ch} {line}")

    work_var.trace_add("write", refresh_meta)
    novel_var.trace_add("write", refresh_meta)

    nb = ttk.Notebook(outer)
    nb.pack(fill=tk.BOTH, expand=True, pady=(0, 6))

    tab_plot = ttk.Frame(nb, padding=4)
    tab_tts = ttk.Frame(nb, padding=4)
    nb.add(tab_plot, text="1. 줄거리 → BRIEF")
    nb.add(tab_tts, text="2. BRIEF → 합본 · tts 저장")

    head = ttk.Frame(tab_plot)
    head.pack(fill=tk.X)
    ttk.Label(head, text="줄거리 (~700자)").pack(side=tk.LEFT)
    ttk.Label(head, textvariable=plot_count_var).pack(side=tk.RIGHT)

    plot_txt = scrolledtext.ScrolledText(tab_plot, height=7, wrap=tk.WORD)
    plot_txt.pack(fill=tk.BOTH, expand=False, pady=(2, 6))

    def on_plot_key(_e: object | None = None) -> None:
        plot_count_var.set(f"{len(plot_txt.get('1.0', 'end-1c'))}자")

    plot_txt.bind("<KeyRelease>", on_plot_key)

    ttk.Label(tab_plot, text="BRIEF 미리보기").pack(anchor="w")
    preview = scrolledtext.ScrolledText(tab_plot, height=10, wrap=tk.WORD)
    preview.pack(fill=tk.BOTH, expand=True, pady=(2, 4))

    state: dict[str, str] = {"brief": ""}

    def _build_from_plot() -> str | None:
        try:
            ch = _chapter_int()
        except ValueError as e:
            messagebox.showwarning("작업 루트", str(e))
            diag_log.log(f"BRIEF 중단: {e}")
            return None
        plot = plot_txt.get("1.0", "end-1c").strip()
        if not plot:
            messagebox.showwarning("줄거리", "줄거리를 입력하세요.")
            diag_log.log("BRIEF 중단: 줄거리 비어 있음")
            return None
        _set_progress(1, 4, "메타 로드")
        novel = Path(novel_var.get().strip())
        meta = load_chapter_meta(novel, ch) if novel.is_dir() else load_chapter_meta("", ch)
        refresh_meta()
        diag_log.log(
            f"BRIEF 빌드 ch={ch} plot={len(plot)}자 "
            f"title={meta.body_title!r} events={meta.event_ids}"
        )
        _set_progress(2, 4, "BRIEF 조립")
        inp = BriefInput(
            chapter=ch,
            body_title=meta.body_title,
            youtube_title=meta.youtube_title,
            age=meta.age,
            season=meta.season,
            place="",
            event_ids=list(meta.event_ids),
            plot=plot,
            cast="",
            prev_state="",
            ending_hook="",
        )
        brief = build_brief_markdown(inp)
        state["brief"] = brief
        _persist()
        _set_progress(3, 4, f"BRIEF {len(brief)}자")
        return brief

    def _save_brief_file(brief: str) -> Path | None:
        novel = Path(novel_var.get().strip())
        try:
            ch = _chapter_int()
        except ValueError:
            diag_log.log("BRIEF 저장 실패: 장 번호")
            return None
        out = (
            brief_path(novel, ch)
            if novel.is_dir()
            else default_output_dir(novel) / f"CHAPTER_{ch:02d}.md"
        )
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(brief, encoding="utf-8")
        diag_log.log(f"BRIEF 저장 path={out} chars={len(brief)}")
        return out

    def _maybe_sync_bible(plot: str) -> str:
        if not sync_bible_var.get():
            return ""
        novel = Path(novel_var.get().strip())
        if not novel.is_dir():
            return "동기화 생략(작품 폴더 없음)"
        try:
            ch = _chapter_int()
        except ValueError:
            return "동기화 생략(장 번호)"
        meta = load_chapter_meta(novel, ch)
        result = sync_plot_to_bible(
            novel,
            ch,
            plot,
            body_title=meta.body_title,
            youtube_title=meta.youtube_title,
            event_ids=list(meta.event_ids),
        )
        msg = result.summary()
        diag_log.log(msg)
        return msg

    def do_brief() -> None:
        try:
            brief = _build_from_plot()
            if not brief:
                _idle("중단")
                return
            preview.delete("1.0", "end")
            preview.insert("1.0", brief)
            out = _save_brief_file(brief)
            if out is None:
                status_var.set("BRIEF 생성됨 · 저장 실패(작업 루트 …/N장 확인)")
                _idle("저장 실패")
                return
            plot = plot_txt.get("1.0", "end-1c").strip()
            sync_msg = _maybe_sync_bible(plot)
            _set_progress(4, 4, "저장 완료")
            status_var.set(
                f"BRIEF 생성·자동 저장: {out}"
                + (f"\n{sync_msg}" if sync_msg else "")
            )
            refresh_meta()
            _idle("BRIEF 완료")
        except Exception as ex:
            diag_log.log(f"BRIEF 예외: {ex!r}")
            _idle("오류")
            messagebox.showerror("BRIEF", str(ex))

    def do_save_brief() -> None:
        text = preview.get("1.0", "end-1c").strip()
        if not text:
            if not _build_from_plot():
                _idle("중단")
                return
            text = state["brief"]
        else:
            state["brief"] = text
        out = _save_brief_file(text)
        if out is None:
            _idle("저장 실패")
            return
        plot = plot_txt.get("1.0", "end-1c").strip()
        sync_msg = _maybe_sync_bible(plot)
        status_var.set(f"BRIEF 저장: {out}" + (f"\n{sync_msg}" if sync_msg else ""))
        refresh_meta()
        _idle("저장 완료")

    act1 = ttk.Frame(tab_plot)
    act1.pack(fill=tk.X)
    ttk.Checkbutton(
        act1,
        text="줄거리 반영 (chapter_map · events · 부줄거리)",
        variable=sync_bible_var,
        command=_persist,
    ).pack(side=tk.LEFT, padx=(0, 12))
    ttk.Button(act1, text="BRIEF 생성·저장", command=do_brief).pack(side=tk.LEFT, padx=(0, 6))
    ttk.Button(act1, text="미리보기 다시 저장", command=do_save_brief).pack(side=tk.LEFT)
    ttk.Button(
        act1,
        text="원클릭: 대본 생성",
        command=lambda: do_oneclick_write_tts(),
    ).pack(side=tk.RIGHT)

    ttk.Label(
        tab_tts,
        text=(
            "「원클릭: 대본 생성」한 번으로 합본→젠스파크 전송→대본 회수→tts 저장까지 합니다. "
            f"(수동: 합본 복사 · 결과 파일 → tts / {CHAPTER_START}…)"
        ),
        wraplength=820,
    ).pack(anchor="w", pady=(0, 4))

    # 버튼은 위쪽에 두 줄 — 아래 텍스트·로그에 가리지 않음
    act_wrap = ttk.Frame(tab_tts)
    act_wrap.pack(fill=tk.X, pady=(0, 6))
    act2a = ttk.Frame(act_wrap)
    act2a.pack(fill=tk.X)
    act2b = ttk.Frame(act_wrap)
    act2b.pack(fill=tk.X, pady=(4, 0))
    cred_fr = ttk.Frame(act_wrap)
    cred_fr.pack(fill=tk.X, pady=(4, 0))
    ttk.Label(cred_fr, text="Google").pack(side=tk.LEFT)
    ttk.Entry(cred_fr, textvariable=email_var, width=28).pack(side=tk.LEFT, padx=(6, 4))
    ttk.Entry(cred_fr, textvariable=pw_var, show="*", width=16).pack(side=tk.LEFT, padx=(0, 6))
    ttk.Label(cred_fr, text="(1_5·2_4 계정 자동 재사용 가능)", foreground="#666").pack(
        side=tk.LEFT
    )

    pkt_fr = ttk.LabelFrame(tab_tts, text="젠스파크 합본", padding=4)
    pkt_fr.pack(fill=tk.BOTH, expand=True, pady=(0, 6))
    packet_text = scrolledtext.ScrolledText(pkt_fr, height=5, wrap=tk.WORD)
    packet_text.pack(fill=tk.BOTH, expand=True)

    body_fr = ttk.LabelFrame(tab_tts, text="대본 본문 → tts/{장}.txt", padding=4)
    body_fr.pack(fill=tk.BOTH, expand=True, pady=(0, 4))
    bh = ttk.Frame(body_fr)
    bh.pack(fill=tk.X)
    ttk.Label(bh, text="추출된 본문").pack(side=tk.LEFT)
    watch_var = tk.BooleanVar(value=True)
    ttk.Checkbutton(bh, text="클립보드 감시(START/END)", variable=watch_var).pack(
        side=tk.LEFT, padx=(12, 0)
    )
    ttk.Label(bh, textvariable=body_count_var).pack(side=tk.RIGHT)
    body_text = scrolledtext.ScrolledText(body_fr, height=6, wrap=tk.WORD)
    body_text.pack(fill=tk.BOTH, expand=True, pady=(2, 0))

    def on_body_key(_e: object | None = None) -> None:
        body_count_var.set(f"{len(body_text.get('1.0', 'end-1c')):,}자")

    body_text.bind("<KeyRelease>", on_body_key)

    clip_state: dict[str, str] = {
        "last_saved_hash": "",
        "ignore_clip": "",
        "last_reject_key": "",
    }

    def _log_reject_once(source: str, key: str, msg: str) -> None:
        if clip_state.get("last_reject_key") == key:
            return
        clip_state["last_reject_key"] = key
        diag_log.log(msg)
        status_var.set(msg.split(":", 1)[-1].strip() if ":" in msg else msg)

    def _try_save_from_text(raw: str, *, source: str, allow_raw: bool = False) -> bool:
        if raw and (
            raw == clip_state.get("ignore_clip") or looks_like_genspark_packet(raw)
        ):
            _log_reject_once(
                source,
                f"packet:{len(raw)}",
                f"{source}: 클립보드가 합본(프롬프트)입니다. "
                "젠스파크 응답을 복사하세요.",
            )
            return False
        kind, body = resolve_clipboard_body(raw)
        if kind == "placeholder":
            _log_reject_once(
                source,
                f"ph:{len(raw)}",
                f"{source}: 예시 START/END만 있음 — 젠스파크 대본을 복사하세요.",
            )
            return False
        if kind == "no_markers":
            if allow_raw and (raw or "").strip():
                body = raw.replace("\r\n", "\n").strip()
            else:
                _log_reject_once(
                    source,
                    f"nomark:{len(raw)}",
                    f"{source}: START/END 없음 clip={len(raw)}자 "
                    f"(젠스파크가 {CHAPTER_START}로 감싸야 함)",
                )
                return False
        if not body:
            return False
        try:
            ch = _chapter_int()
        except ValueError:
            diag_log.log(f"{source}: 장 번호 오류")
            return False
        work = work_var.get().strip()
        if not work:
            status_var.set("작업 루트를 지정하세요 (START/END 감지됨)")
            diag_log.log(f"{source}: 작업 루트 없음")
            return False
        key = f"{ch}:{len(body)}:{hash(body)}"
        if key == clip_state["last_saved_hash"]:
            return True
        _set_progress(1, 2, "tts 저장 중")
        path = save_chapter_to_tts(
            work, ch, body, novel_root=novel_var.get().strip() or None
        )
        clip_state["last_saved_hash"] = key
        clip_state["last_reject_key"] = ""
        saved_body = path.read_text(encoding="utf-8-sig")
        body_text.delete("1.0", tk.END)
        body_text.insert("1.0", saved_body)
        on_body_key()
        touch_workspace_from_path(work)
        _persist()
        _set_progress(2, 2, "tts 저장 완료")
        status_var.set(f"{source} → tts/{ch}.txt ({len(saved_body):,}자) · {path}")
        diag_log.log(f"{source} → {path} chars={len(saved_body)}")
        _idle("tts 저장 완료")
        return True

    def do_clipboard_save() -> None:
        try:
            raw = root.clipboard_get()
        except tk.TclError:
            messagebox.showwarning("클립보드", "클립보드에 텍스트가 없습니다.")
            diag_log.log("클립보드 비어 있음")
            return
        diag_log.log(f"클립보드 수동 저장 시도 chars={len(raw)}")
        if _try_save_from_text(raw, source="클립보드"):
            return
        if looks_like_genspark_packet(raw):
            messagebox.showinfo(
                "합본입니다",
                "지금 클립보드는 합본(젠스파크에 넣을 프롬프트)입니다.\n\n"
                "1) 합본을 젠스파크에 붙여넣기\n"
                "2) 대본이 나오면 그 결과만 복사\n"
                f"3) 결과에 {CHAPTER_START} … {CHAPTER_END} 가 있으면 자동 저장됩니다.",
            )
            return
        kind, _ = resolve_clipboard_body(raw)
        if kind == "no_markers" and len((raw or "").strip()) >= 500:
            if messagebox.askyesno(
                "구분자 없음",
                f"클립보드에 {CHAPTER_START} / {CHAPTER_END} 가 없습니다.\n\n"
                f"전체 {len(raw.strip()):,}자를 대본으로 저장할까요?",
            ):
                _try_save_from_text(raw, source="클립보드(전체)", allow_raw=True)
            return
        messagebox.showwarning(
            "구분자 없음",
            f"클립보드에 {CHAPTER_START} / {CHAPTER_END} 가 없습니다.\n"
            "젠스파크 대본 결과를 복사한 뒤 다시 시도하세요.",
        )

    def _poll_clipboard() -> None:
        if not standalone:
            return
        try:
            if watch_var.get():
                try:
                    raw = root.clipboard_get()
                except tk.TclError:
                    raw = ""
                if raw and (
                    CHAPTER_START in raw
                    or looks_like_genspark_packet(raw)
                ):
                    _try_save_from_text(raw, source="클립보드 감시")
        except tk.TclError:
            pass
        try:
            root.after(1500, _poll_clipboard)
        except tk.TclError:
            pass

    def do_build_packet() -> None:
        try:
            ch = _chapter_int()
        except ValueError as e:
            messagebox.showerror("작업 루트", str(e))
            return
        novel = novel_var.get().strip()
        work = work_var.get().strip()
        if not novel or not Path(novel).is_dir():
            messagebox.showerror("작품 폴더", "작품 폴더를 선택하세요.")
            return
        if not work:
            messagebox.showerror("작업 루트", "작업 루트(…/N장)를 선택하세요.")
            return
        _busy("합본 조립…")
        try:
            bp = resolve_brief_file(novel, ch)
            if bp is None:
                diag_log.log(f"합본: BRIEF 없음 ch={ch}")
                messagebox.showwarning(
                    "BRIEF 없음",
                    f"briefs/CHAPTER_{ch}.md 가 없습니다.\n탭1에서 BRIEF를 저장하세요.",
                )
            else:
                diag_log.log(f"합본: BRIEF={bp}")
            _set_progress(1, 3, "합본 생성")
            packet, notes = build_packet_from_brief(
                novel_root=novel, work_root=work, chapter=ch
            )
            packet_text.delete("1.0", tk.END)
            packet_text.insert("1.0", packet)
            msg = f"합본 {len(packet):,}자"
            if notes:
                msg += " · " + "; ".join(notes)
                diag_log.log(f"합본 notes={notes}")
            status_var.set(msg)
            diag_log.log(f"합본 완료 chars={len(packet)} ch={ch}")
            _set_progress(3, 3, "합본 완료")
            _persist()
            _idle("합본 완료")
        except Exception as ex:
            diag_log.log(f"합본 예외: {ex!r}")
            _idle("합본 오류")
            messagebox.showerror("합본", str(ex))

    def do_copy_packet() -> None:
        data = packet_text.get("1.0", "end-1c")
        if not data.strip():
            do_build_packet()
            data = packet_text.get("1.0", "end-1c")
        if not data.strip():
            return
        _clipboard_set(root, data)
        # 합본에도 예시 START/END가 있어 감시가 오인 저장하지 않도록 무시
        clip_state["ignore_clip"] = data
        opened = _open_genspark()
        diag_log.log(f"합본 클립보드 복사 chars={len(data)} (감시 무시 등록)")
        if opened:
            status_var.set(
                f"합본 복사 · 젠스파크 열림 ({len(data):,}자). 붙여넣기 후 결과 다운로드."
            )
        else:
            status_var.set(
                f"합본 복사됨 ({len(data):,}자). 브라우저가 안 열리면 "
                "「젠스파크 열기」를 누르세요."
            )

    def do_open_genspark() -> None:
        if _open_genspark():
            status_var.set("젠스파크 브라우저를 열었습니다.")
        else:
            messagebox.showwarning(
                "브라우저",
                f"브라우저를 열지 못했습니다.\n직접 열어 주세요:\n{GENSPARK_AI_CHAT_URL}",
            )

    def _resolve_account() -> tuple[str, str] | None:
        email = email_var.get().strip()
        password = pw_var.get()
        if not password:
            _e, _p = load_credentials()
            if not email and _e:
                email = _e
                email_var.set(_e)
            if _p:
                password = _p
                pw_var.set(_p)
        if not email or not password:
            messagebox.showwarning(
                "로그인",
                "Genspark Google 이메일·비밀번호를 입력하세요.\n"
                "(다른 모듈 dist 자격증명이 있으면 자동으로 불러옵니다.)",
            )
            return None
        save_credentials(email, password)
        return email, password

    def do_run_genspark_chapter() -> None:
        from wisdom_gui_host import safe_after, safe_messagebox

        if run_busy["on"]:
            return
        try:
            ch = _chapter_int()
        except ValueError as e:
            messagebox.showerror("작업 루트", str(e))
            return
        work = work_var.get().strip()
        if not work:
            messagebox.showerror("작업 루트", "작업 루트(…/N장)를 선택하세요.")
            return
        data = packet_text.get("1.0", "end-1c")
        if not data.strip():
            do_build_packet()
            data = packet_text.get("1.0", "end-1c")
        if not data.strip():
            messagebox.showwarning("합본", "합본이 비어 있습니다. 먼저 합본 만들기를 하세요.")
            return
        acc = _resolve_account()
        if acc is None:
            return
        email, password = acc

        try:
            from plot_to_prompt.genspark_chat import has_playwright, run_chapter_flow
        except ImportError as e:
            messagebox.showerror(
                "모듈 없음",
                "plot_to_prompt.genspark_chat 를 불러오지 못했습니다.\n"
                f"{e}\nexe를 다시 빌드하세요.",
            )
            diag_log.log(f"genspark_chat import 실패: {e!r}")
            return

        if not has_playwright():
            messagebox.showerror(
                "Playwright",
                "Playwright가 없습니다.\n"
                "Python에 pip install playwright 후\n"
                "playwright install chromium 을 실행하세요.",
            )
            return

        clip_state["ignore_clip"] = data
        run_busy["on"] = True
        _busy("젠스파크 작성 중…")
        status_var.set("젠스파크에 합본 붙여넣기·전송·대기 중…")
        diag_log.log(f"젠스파크 실행 시작 ch={ch} packet={len(data)}자")
        work_root = work
        novel_root = novel_var.get().strip() or None

        def _genspark_worker() -> None:
            try:
                result = run_chapter_flow(
                    packet_text=data,
                    profile_dir=genspark_profile_dir(),
                    open_browser=True,
                    url=GENSPARK_AI_CHAT_URL,
                    email=email,
                    password=password,
                )
                body = (result.get("chapter_text") or "").strip()
                saved_path: Path | None = None
                save_err: BaseException | None = None
                if body:
                    try:
                        saved_path = save_chapter_to_tts(
                            work_root,
                            ch,
                            body,
                            novel_root=novel_root,
                        )
                        diag_log.log(
                            f"젠스파크 저장완료 → {saved_path} chars={len(body)}"
                        )
                    except BaseException as se:
                        save_err = se
                        diag_log.log(f"젠스파크 저장실패: {se!r}")

                def done() -> None:
                    run_busy["on"] = False
                    if not body:
                        _idle("대본 없음")
                        safe_messagebox(
                            root,
                            "showwarning",
                            "대본 없음",
                            "응답에서 대본을 찾지 못했습니다.\n"
                            "「결과 파일 → tts」로 수동 저장하세요.",
                        )
                        return
                    if save_err is not None or saved_path is None:
                        _idle("저장 오류")
                        safe_messagebox(
                            root,
                            "showerror",
                            "저장 실패",
                            f"대본은 받았으나 파일 저장에 실패했습니다.\n{save_err}",
                        )
                        body_text.delete("1.0", tk.END)
                        body_text.insert("1.0", body)
                        on_body_key()
                        return
                    saved_body = saved_path.read_text(encoding="utf-8-sig")
                    body_text.delete("1.0", tk.END)
                    body_text.insert("1.0", saved_body)
                    on_body_key()
                    touch_workspace_from_path(work_root)
                    _persist()
                    note = " · 로그인OK" if result.get("logged_in") else ""
                    status_var.set(
                        f"젠스파크 → tts/{ch}.txt ({len(saved_body):,}자){note} · {saved_path}"
                    )
                    diag_log.log(
                        f"젠스파크 → {saved_path} chars={len(saved_body)}"
                    )
                    _idle("tts 저장 완료")
                    safe_messagebox(
                        root,
                        "showinfo",
                        "완료",
                        f"대본 저장 완료{note}\n{saved_path}\n{len(saved_body):,}자",
                    )

                safe_after(root, done)
            except Exception as e:
                def fail() -> None:
                    run_busy["on"] = False
                    diag_log.log(f"젠스파크 실행 오류: {e!r}")
                    _idle("젠스파크 오류")
                    safe_messagebox(root, "showerror", "젠스파크", str(e))

                safe_after(root, fail)

        threading.Thread(target=_genspark_worker, daemon=True).start()

    def do_oneclick_write_tts() -> None:
        """원클릭: (가능하면 BRIEF 갱신) -> 합본 -> 젠스파크 -> tts 저장."""
        if run_busy["on"]:
            messagebox.showinfo(
                "진행 중",
                "이미 젠스파크 작성이 진행 중입니다.\n"
                "끝날 때까지 기다리거나, Google 로그인 팝업을 처리한 뒤 다시 시도하세요.",
            )
            return
        try:
            _chapter_int()
        except ValueError as e:
            messagebox.showerror("작업 루트", str(e))
            return
        work = work_var.get().strip()
        if not work:
            messagebox.showerror("작업 루트", "작업 루트(…/N장)를 선택하세요.")
            return
        acc = _resolve_account()
        if acc is None:
            return
        # 줄거리가 있으면 최신 BRIEF를 먼저 갱신
        plot_raw = plot_txt.get("1.0", "end-1c").strip()
        if plot_raw:
            do_brief()
        do_build_packet()
        data = packet_text.get("1.0", "end-1c").strip()
        if not data:
            messagebox.showwarning("합본", "합본이 비어 있습니다. BRIEF/줄거리를 확인하세요.")
            return
        status_var.set("원클릭: 합본 완료 → 젠스파크 대본 작성 시작…")
        do_run_genspark_chapter()

    def do_save_tts() -> None:
        try:
            ch = _chapter_int()
        except ValueError as e:
            messagebox.showerror("작업 루트", str(e))
            return
        work = work_var.get().strip()
        if not work:
            messagebox.showerror("작업 루트", "작업 루트(…/N장)를 선택하세요.")
            return
        raw = body_text.get("1.0", "end-1c")
        body = extract_chapter_body(raw) or raw.strip()
        if not body:
            messagebox.showwarning("대본", "저장할 본문이 비어 있습니다.")
            return
        path = save_chapter_to_tts(
            work, ch, body, novel_root=novel_var.get().strip() or None
        )
        saved_body = path.read_text(encoding="utf-8-sig")
        body_text.delete("1.0", tk.END)
        body_text.insert("1.0", saved_body)
        on_body_key()
        touch_workspace_from_path(work)
        _persist()
        status_var.set(f"저장: {path} ({len(saved_body):,}자)")
        diag_log.log(f"칸 내용 저장 {path} chars={len(saved_body)}")
        _idle("tts 저장 완료")

    def do_load_tts() -> None:
        try:
            ch = _chapter_int()
        except ValueError as e:
            messagebox.showerror("작업 루트", str(e))
            return
        work = work_var.get().strip()
        path = chapter_tts_path(work, ch) if work else None
        if path is None or not path.is_file():
            messagebox.showinfo("없음", f"tts/{ch}.txt 가 없습니다.")
            diag_log.log(f"tts 없음 ch={ch}")
            return
        body_text.delete("1.0", tk.END)
        body_text.insert("1.0", path.read_text(encoding="utf-8-sig"))
        on_body_key()
        status_var.set(f"불러옴: {path}")
        diag_log.log(f"tts 불러옴 {path}")

    def do_import_genspark_file() -> None:
        try:
            ch = _chapter_int()
        except ValueError as e:
            messagebox.showerror("작업 루트", str(e))
            return
        work = work_var.get().strip()
        if not work:
            messagebox.showerror("작업 루트", "작업 루트(…/N장)를 선택하세요.")
            return
        downloads = Path.home() / "Downloads"
        initial = str(downloads) if downloads.is_dir() else folder_dialog_initial()
        path_str = filedialog.askopenfilename(
            title="젠스파크 결과 파일 선택",
            initialdir=initial,
            filetypes=[
                ("텍스트 / 마크다운", "*.txt *.md *.markdown"),
                ("모든 파일", "*.*"),
            ],
        )
        if not path_str:
            return
        src = Path(path_str)
        try:
            raw = read_export_text(src)
        except OSError as ex:
            messagebox.showerror("파일", f"읽기 실패: {ex}")
            diag_log.log(f"결과 파일 읽기 실패: {ex!r}")
            return
        kind, body = body_from_export(raw)
        diag_log.log(f"결과 파일={src.name} chars={len(raw)} kind={kind}")
        if kind == "packet":
            messagebox.showwarning(
                "합본 파일",
                "선택한 파일은 합본(프롬프트)입니다.\n"
                "젠스파크에서 받은 대본 결과 파일을 선택하세요.",
            )
            return
        if kind in ("empty", "placeholder") or not body.strip():
            messagebox.showwarning("내용 없음", "파일에서 대본을 찾지 못했습니다.")
            return
        if kind == "raw":
            diag_log.log(f"결과 파일: START/END 없음 → 전체 {len(body)}자 저장")
        path = save_chapter_to_tts(
            work, ch, body, novel_root=novel_var.get().strip() or None
        )
        saved_body = path.read_text(encoding="utf-8-sig")
        body_text.delete("1.0", tk.END)
        body_text.insert("1.0", saved_body)
        on_body_key()
        touch_workspace_from_path(work)
        _persist()
        status_var.set(f"결과 파일 → tts/{ch}.txt ({len(saved_body):,}자) · {path}")
        diag_log.log(f"결과 파일 → {path} chars={len(saved_body)}")
        _idle("tts 저장 완료")

    ttk.Button(
        act2a,
        text="원클릭: 대본 생성",
        command=do_oneclick_write_tts,
    ).pack(side=tk.LEFT, padx=(0, 8))
    ttk.Separator(act2a, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=6)
    ttk.Button(act2a, text="합본 만들기", command=do_build_packet).pack(side=tk.LEFT, padx=(0, 6))
    ttk.Button(act2a, text="젠스파크만 실행", command=do_run_genspark_chapter).pack(
        side=tk.LEFT, padx=(0, 6)
    )
    ttk.Button(act2a, text="합본 복사 · 열기", command=do_copy_packet).pack(
        side=tk.LEFT, padx=(0, 6)
    )
    ttk.Button(act2a, text="젠스파크 열기", command=do_open_genspark).pack(side=tk.LEFT, padx=(0, 6))
    ttk.Button(act2b, text="결과 파일 → tts", command=do_import_genspark_file).pack(
        side=tk.LEFT, padx=(0, 6)
    )
    ttk.Button(act2b, text="START/END로 저장", command=do_clipboard_save).pack(
        side=tk.LEFT, padx=(0, 6)
    )
    ttk.Button(act2b, text="tts에서 불러오기", command=do_load_tts).pack(side=tk.LEFT, padx=(0, 6))
    ttk.Button(act2b, text="칸 내용 저장", command=do_save_tts).pack(side=tk.LEFT)

    def on_close() -> None:
        try:
            diag_log.log("종료")
            diag_log.remove_listener(_append_log_line)
            _persist()
        except Exception:
            pass

    bind_close(root, standalone, on_close=on_close)
    refresh_meta()
    on_plot_key()
    on_body_key()
    if standalone:
        root.after(1500, _poll_clipboard)
    run_mainloop(root, standalone)


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""2_3_1_mp4ToSrt GUI — MP4 업로드 → Whisper → SRT."""

from __future__ import annotations

import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, font as tkfont, ttk

from mp4_to_srt import __version__
from mp4_to_srt.settings import (
    MODEL_CHOICES,
    default_output_dir,
    folder_dialog_initial,
    list_video_files,
    load_gui_settings,
    save_gui_settings,
)
from mp4_to_srt.whisper_stt import has_faster_whisper, transcribe_to_srt_text
from wisdom_workspace import touch_workspace_from_path


def _default_font() -> tuple[str, int]:
    try:
        f = tkfont.nametofont("TkDefaultFont")
        return (f.actual("family"), max(10, int(f.actual("size"))))
    except tk.TclError:
        return ("맑은 고딕", 10)


def main(*, container: tk.Misc | None = None) -> None:
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
    if not standalone and getattr(root, "_mp4_to_srt_gui_built", False):
        return
    if not standalone:
        setattr(root, "_mp4_to_srt_gui_built", True)

    apply_window_chrome(
        root,
        standalone,
        title=f"2_3_1 mp4ToSrt {__version__}",
        minsize=(680, 420),
        geometry="820x480",
    )
    fam, sz = _default_font()
    root.option_add("*Font", (fam, sz))

    cfg = load_gui_settings()
    mp4_default = cfg.get("mp4_path") or ""
    folder_default = cfg.get("mp4_folder") or ""
    out_default = cfg.get("output_dir") or str(default_output_dir())
    model_var = tk.StringVar(value=cfg.get("whisper_model") or "base")
    if model_var.get() not in MODEL_CHOICES:
        model_var.set("base")
    lang_var = tk.StringVar(value=cfg.get("language") or "ko")
    beside_default = (cfg.get("srt_beside") or "1").strip() not in ("0", "false", "no")

    mp4_var = tk.StringVar(value=mp4_default)
    folder_var = tk.StringVar(value=folder_default)
    out_var = tk.StringVar(value=out_default)
    beside_var = tk.BooleanVar(value=beside_default)
    batch_var = tk.BooleanVar(value=bool(folder_default) and not mp4_default)
    status_var = tk.StringVar(value="MP4 선택 → Whisper 인식 → SRT 저장")
    prog_var = tk.DoubleVar(value=0.0)
    busy = {"v": False}

    frm = ttk.Frame(root, padding=10)
    frm.pack(fill=tk.BOTH, expand=True)
    frm.grid_columnconfigure(1, weight=1)

    def persist() -> None:
        save_gui_settings(
            mp4_path=mp4_var.get().strip(),
            mp4_folder=folder_var.get().strip(),
            output_dir=out_var.get().strip(),
            whisper_model=model_var.get().strip(),
            language=lang_var.get().strip() or "ko",
            srt_beside=bool(beside_var.get()),
        )

    def set_status(msg: str) -> None:
        status_var.set(msg)

    def set_progress(pct: float, msg: str = "") -> None:
        prog_var.set(max(0.0, min(100.0, float(pct))))
        if msg:
            status_var.set(msg)

    def set_busy(v: bool) -> None:
        busy["v"] = v
        st = tk.DISABLED if v else tk.NORMAL
        for b in (btn_mp4, btn_folder, btn_out, btn_run, btn_refresh):
            try:
                b.configure(state=st)
            except tk.TclError:
                pass

    def resolve_targets() -> list[Path]:
        if batch_var.get():
            folder = Path(folder_var.get().strip() or ".")
            files = list_video_files(folder)
            if not files:
                raise ValueError(f"폴더에 영상 파일이 없습니다.\n{folder}")
            return files
        p = Path(mp4_var.get().strip())
        if not p.is_file():
            raise ValueError("MP4(영상) 파일을 선택하세요.")
        return [p]

    def srt_dest_for(video: Path) -> Path:
        if beside_var.get():
            return video.with_suffix(".srt")
        out = Path(out_var.get().strip() or str(default_output_dir()))
        out.mkdir(parents=True, exist_ok=True)
        return out / f"{video.stem}.srt"

    ttk.Label(frm, text="모드", width=12).grid(row=0, column=0, sticky="w")
    mode_fr = ttk.Frame(frm)
    mode_fr.grid(row=0, column=1, sticky="w", padx=4)

    def on_mode() -> None:
        batch = bool(batch_var.get())
        st_file = tk.DISABLED if batch else tk.NORMAL
        st_batch = tk.NORMAL if batch else tk.DISABLED
        try:
            mp4_ent.configure(state=st_file)
            btn_mp4.configure(state=st_file if not busy["v"] else tk.DISABLED)
            folder_ent.configure(state=st_batch)
            btn_folder.configure(state=st_batch if not busy["v"] else tk.DISABLED)
            btn_refresh.configure(state=st_batch if not busy["v"] else tk.DISABLED)
        except tk.TclError:
            pass

    ttk.Radiobutton(
        mode_fr, text="단일 MP4", variable=batch_var, value=False, command=on_mode
    ).pack(side=tk.LEFT)
    ttk.Radiobutton(
        mode_fr, text="폴더 일괄", variable=batch_var, value=True, command=on_mode
    ).pack(side=tk.LEFT, padx=(12, 0))

    ttk.Label(frm, text="MP4 파일", width=12).grid(row=1, column=0, sticky="w", pady=(6, 0))
    mp4_ent = ttk.Entry(frm, textvariable=mp4_var)
    mp4_ent.grid(row=1, column=1, sticky="ew", padx=4, pady=(6, 0))

    def pick_mp4() -> None:
        init = folder_dialog_initial(
            Path(mp4_var.get()).parent if mp4_var.get().strip() else None
        )
        p = filedialog.askopenfilename(
            parent=root,
            title="MP4 영상 선택",
            initialdir=init,
            filetypes=[
                ("Video", "*.mp4;*.mkv;*.webm;*.mov;*.m4v;*.avi"),
                ("MP4", "*.mp4"),
                ("All", "*.*"),
            ],
        )
        if p:
            mp4_var.set(p)
            batch_var.set(False)
            on_mode()
            touch_workspace_from_path(p)
            persist()
            set_status(f"선택 → {p}")

    def on_mp4_drop(path: str) -> None:
        p = Path(path)
        if p.is_file():
            mp4_var.set(str(p))
            batch_var.set(False)
            on_mode()
            touch_workspace_from_path(str(p))
            persist()
        elif p.is_dir():
            folder_var.set(str(p))
            batch_var.set(True)
            on_mode()
            touch_workspace_from_path(str(p))
            persist()
            set_status(f"폴더 → {p}")

    btn_mp4 = ttk.Button(frm, text="찾기", command=pick_mp4, width=8)
    btn_mp4.grid(row=1, column=2, padx=(4, 0), pady=(6, 0))
    bind_path_row_dnd(mp4_ent, frm, mp4_var, mode="file", on_set=on_mp4_drop)
    bind_path_entry_dnd(mp4_ent, mp4_var, mode="file", on_set=on_mp4_drop)

    ttk.Label(frm, text="영상 폴더", width=12).grid(row=2, column=0, sticky="w", pady=(6, 0))
    folder_ent = ttk.Entry(frm, textvariable=folder_var)
    folder_ent.grid(row=2, column=1, sticky="ew", padx=4, pady=(6, 0))

    def pick_folder() -> None:
        init = folder_dialog_initial(
            Path(folder_var.get().strip()) if folder_var.get().strip() else None
        )
        d = filedialog.askdirectory(
            parent=root, title="영상 폴더 (일괄)", initialdir=init
        )
        if d:
            folder_var.set(d)
            batch_var.set(True)
            on_mode()
            touch_workspace_from_path(d)
            persist()
            n = len(list_video_files(d))
            set_status(f"폴더 영상 {n}개 → {d}")

    def on_folder_drop(_path: str) -> None:
        p = folder_var.get().strip()
        if not p:
            return
        batch_var.set(True)
        on_mode()
        touch_workspace_from_path(p)
        persist()
        n = len(list_video_files(p))
        set_status(f"폴더 영상 {n}개 → {p}")

    btn_folder = ttk.Button(frm, text="찾기", command=pick_folder, width=8)
    btn_folder.grid(row=2, column=2, padx=(4, 0), pady=(6, 0))
    bind_path_row_dnd(folder_ent, frm, folder_var, mode="dir", on_set=on_folder_drop)
    bind_path_entry_dnd(folder_ent, folder_var, mode="dir", on_set=on_folder_drop)

    def do_refresh() -> None:
        d = folder_var.get().strip()
        if not d:
            set_status("영상 폴더를 지정하세요.")
            return
        n = len(list_video_files(d))
        set_status(f"폴더 영상 {n}개 → {d}")

    btn_refresh = ttk.Button(frm, text="목록", command=do_refresh, width=8)

    ttk.Label(frm, text="SRT 저장", width=12).grid(row=3, column=0, sticky="w", pady=(6, 0))
    save_fr = ttk.Frame(frm)
    save_fr.grid(row=3, column=1, sticky="w", padx=4, pady=(6, 0))

    def on_beside() -> None:
        st = tk.DISABLED if beside_var.get() else tk.NORMAL
        try:
            out_ent.configure(state=st)
            btn_out.configure(state=st if not busy["v"] else tk.DISABLED)
        except tk.TclError:
            pass
        persist()

    ttk.Checkbutton(
        save_fr,
        text="영상과 같은 폴더",
        variable=beside_var,
        command=on_beside,
    ).pack(side=tk.LEFT)

    ttk.Label(frm, text="출력 폴더", width=12).grid(row=4, column=0, sticky="w", pady=(6, 0))
    out_ent = ttk.Entry(frm, textvariable=out_var)
    out_ent.grid(row=4, column=1, sticky="ew", padx=4, pady=(6, 0))

    def pick_out() -> None:
        init = folder_dialog_initial(
            Path(out_var.get().strip()) if out_var.get().strip() else None
        )
        d = filedialog.askdirectory(parent=root, title="SRT 출력 폴더", initialdir=init)
        if d:
            out_var.set(d)
            beside_var.set(False)
            on_beside()
            touch_workspace_from_path(d)
            persist()

    btn_out = ttk.Button(frm, text="찾기", command=pick_out, width=8)
    btn_out.grid(row=4, column=2, padx=(4, 0), pady=(6, 0))
    bind_path_row_dnd(out_ent, frm, out_var, mode="dir")
    bind_path_entry_dnd(out_ent, out_var, mode="dir")

    ttk.Label(frm, text="Whisper 모델", width=12).grid(row=5, column=0, sticky="w", pady=(6, 0))
    model_cb = ttk.Combobox(
        frm, textvariable=model_var, values=MODEL_CHOICES, state="readonly", width=14
    )
    model_cb.grid(row=5, column=1, sticky="w", padx=4, pady=(6, 0))
    btn_refresh.grid(row=5, column=2, padx=(4, 0), pady=(6, 0))

    ttk.Label(frm, text="언어", width=12).grid(row=6, column=0, sticky="w", pady=(6, 0))
    lang_ent = ttk.Entry(frm, textvariable=lang_var, width=8)
    lang_ent.grid(row=6, column=1, sticky="w", padx=4, pady=(6, 0))

    tip = ttk.Label(
        frm,
        text="MP4 음성을 Whisper로 인식해 SRT로 저장합니다. PATH에 ffmpeg 권장. 한 트랙 20~25자.",
        foreground="#555",
    )
    tip.grid(row=7, column=0, columnspan=3, sticky="w", pady=(10, 0))

    def do_run() -> None:
        if busy["v"]:
            return
        try:
            targets = resolve_targets()
        except ValueError as e:
            safe_messagebox(root, "showwarning", "2_3_1 mp4ToSrt", str(e))
            return
        if not has_faster_whisper():
            safe_messagebox(
                root,
                "showerror",
                "2_3_1 mp4ToSrt",
                "faster-whisper 가 설치되어 있지 않습니다.\n"
                "pip install faster-whisper",
            )
            return
        if not beside_var.get() and not out_var.get().strip():
            safe_messagebox(root, "showwarning", "2_3_1 mp4ToSrt", "출력 폴더를 지정하세요.")
            return
        persist()
        model = model_var.get().strip() or "base"
        lang = lang_var.get().strip() or "ko"
        total = len(targets)

        def work() -> None:
            done_paths: list[Path] = []
            try:
                for i, video in enumerate(targets):
                    base = (i / total) * 100.0 if total else 0.0
                    span = 100.0 / total if total else 100.0

                    def progress(msg: str, pct: float, _base=base, _span=span) -> None:
                        overall = _base + (_span * max(0.0, min(100.0, pct)) / 100.0)
                        safe_after(
                            root,
                            lambda m=msg, p=overall: set_progress(p, m),
                        )

                    progress(f"[{i + 1}/{total}] {video.name}", 1.0)
                    srt = transcribe_to_srt_text(
                        video,
                        model_size=model,
                        language=lang,
                        min_chars=20,
                        max_chars=25,
                        on_progress=progress,
                    )
                    dest = srt_dest_for(video)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_text(srt, encoding="utf-8")
                    done_paths.append(dest)

                def done() -> None:
                    set_busy(False)
                    last = done_paths[-1] if done_paths else Path(".")
                    set_progress(100.0, f"완료 {len(done_paths)}개 → {last}")
                    show_toast(
                        root,
                        f"SRT {len(done_paths)}개 저장\n{last}",
                        title="2_3_1 mp4ToSrt · 완료",
                    )

                safe_after(root, done)
            except Exception as e:
                err = str(e)

                def fail() -> None:
                    set_busy(False)
                    set_progress(0.0, f"오류: {err}")
                    safe_messagebox(root, "showerror", "2_3_1 mp4ToSrt", err)

                safe_after(root, fail)

        set_busy(True)
        set_progress(0.0, "SRT 변환 시작…")
        threading.Thread(target=work, daemon=True).start()

    btn_run = ttk.Button(frm, text="SRT 만들기", command=do_run)
    btn_run.grid(row=8, column=0, columnspan=3, sticky="w", pady=(16, 0))

    prog_fr = ttk.Frame(frm)
    prog_fr.grid(row=9, column=0, columnspan=3, sticky="ew", pady=(12, 0))
    prog_fr.grid_columnconfigure(0, weight=1)
    ttk.Progressbar(
        prog_fr,
        maximum=100,
        mode="determinate",
        variable=prog_var,
    ).grid(row=0, column=0, sticky="ew")
    ttk.Label(prog_fr, textvariable=status_var).grid(
        row=1, column=0, sticky="ew", pady=(4, 0)
    )

    def on_close() -> None:
        persist()

    if standalone:
        bind_close(root, standalone, on_close)
    else:
        bind_hub_destroy(root, on_close)

    on_mode()
    on_beside()
    run_mainloop(root, standalone)

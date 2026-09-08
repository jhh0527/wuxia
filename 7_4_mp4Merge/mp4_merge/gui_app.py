# -*- coding: utf-8 -*-
"""7_4_mp4Merge GUI — 폴더 MP4 고속 병합."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, font as tkfont, ttk

from mp4_merge import __version__
from mp4_merge.listing import list_folder_mp4s
from mp4_merge.log_util import log_file_display, mp4_merge_log, mp4_merge_log_exc
from mp4_merge.merge import merge_folder_to_all
from mp4_merge.paths import ALL_MP4_NAME, MERGE_LOG_NAME, default_mp4_folder
from mp4_merge.settings import load_gui_settings, load_mute_files, save_gui_settings
from wisdom_workspace import folder_dialog_initial, touch_workspace_from_path


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
        bind_path_entry_dnd,
        bind_path_row_dnd,
        configure_notebook_tabs,
        run_mainloop,
        safe_after,
        safe_messagebox,
        show_toast,
        tk_host,
    )

    cfg = load_gui_settings()
    mute_map = load_mute_files()
    mp4_merge_log(f"=== 7_4 mp4Merge {__version__} 시작 log={log_file_display()} ===")

    root, standalone = tk_host(container)
    configure_notebook_tabs(root)
    apply_window_chrome(
        root,
        standalone,
        title=f"7_4 mp4Merge {__version__}",
        minsize=(720, 520),
        geometry="880x640",
    )
    if standalone and sys.platform == "win32":
        try:
            root.state("zoomed")
        except tk.TclError:
            pass

    fam, sz = _default_font()
    root.option_add("*Font", (fam, sz))

    from wisdom_content_paths import infer_root_from_media_path

    mp4_default = str(cfg.get("mp4_folder") or default_mp4_folder())
    root_default = (cfg.get("root_dir") or "").strip()
    if not root_default:
        inferred = infer_root_from_media_path(mp4_default)
        root_default = str(inferred) if inferred is not None else ""

    root_var = tk.StringVar(value=root_default)
    folder_var = tk.StringVar(value=mp4_default)
    status_var = tk.StringVar(value="루트 또는 MP4 폴더를 지정한 뒤 목록을 불러오세요.")
    progress_var = tk.DoubleVar(value=0.0)
    progress_text_var = tk.StringVar(value="")

    busy = {"v": False}
    cancel_event = threading.Event()
    # iid → Path
    rows: dict[str, Path] = {}
    mute_vars: dict[str, tk.StringVar] = {}

    top = ttk.Frame(root, padding=10)
    top.pack(fill=tk.BOTH, expand=True)
    top.columnconfigure(0, weight=1)
    top.rowconfigure(2, weight=1)

    path_fr = ttk.Frame(top)
    path_fr.grid(row=0, column=0, sticky="ew")
    path_fr.columnconfigure(1, weight=1)

    ttk.Label(path_fr, text="루트 폴더", width=10).grid(row=0, column=0, sticky="w")
    root_ent = ttk.Entry(path_fr, textvariable=root_var)
    root_ent.grid(row=0, column=1, sticky="ew", padx=(4, 6))

    ttk.Label(path_fr, text="MP4 폴더", width=10).grid(row=1, column=0, sticky="w", pady=(6, 0))
    folder_ent = ttk.Entry(path_fr, textvariable=folder_var)
    folder_ent.grid(row=1, column=1, sticky="ew", padx=(4, 6), pady=(6, 0))

    def persist() -> None:
        m: dict[str, str] = {}
        for iid, path in rows.items():
            mv = mute_vars.get(iid)
            if mv and mv.get() == "음소거":
                m[path.name.lower()] = "mute"
            else:
                m[path.name.lower()] = "sound"
        save_gui_settings(
            root_dir=root_var.get().strip(),
            mp4_folder=folder_var.get().strip(),
            mute_files=m,
        )

    def apply_root(*, force: bool = True) -> None:
        raw = root_var.get().strip()
        if not raw:
            return
        # mp4만 확보 — mp3/png/jpg/tts 등 불필요 폴더는 만들지 않음
        from wisdom_content_paths import ensure_content_dirs

        r = Path(raw).expanduser()
        layout = ensure_content_dirs(r, "mp4")
        folder_var.set(str(layout["mp4"]))
        if force:
            touch_workspace_from_path(str(r))
        persist()
        refresh_list()
        status_var.set(f"루트 → mp4:{layout['mp4'].name} · {r}")

    def pick_root() -> None:
        cur = root_var.get().strip()
        init = Path(cur) if cur and Path(cur).is_dir() else default_mp4_folder()
        p = filedialog.askdirectory(
            title="루트 폴더",
            initialdir=folder_dialog_initial(init),
        )
        if not p:
            return
        root_var.set(p)
        apply_root(force=True)

    def pick_folder() -> None:
        cur = folder_var.get().strip()
        init = Path(cur) if cur and Path(cur).is_dir() else default_mp4_folder()
        p = filedialog.askdirectory(
            title="MP4 폴더",
            initialdir=folder_dialog_initial(init),
        )
        if not p:
            return
        touch_workspace_from_path(p)
        folder_var.set(p)
        pp = Path(p)
        if pp.name.casefold() == "mp4":
            root_var.set(str(pp.parent))
        persist()
        refresh_list()

    btn_root = ttk.Button(path_fr, text="찾기…", command=pick_root)
    btn_root.grid(row=0, column=2)
    btn_pick = ttk.Button(path_fr, text="찾기…", command=pick_folder)
    btn_pick.grid(row=1, column=2, pady=(6, 0))

    btn_fr = ttk.Frame(top)
    btn_fr.grid(row=1, column=0, sticky="ew", pady=(8, 4))

    def refresh_list() -> None:
        folder = Path(folder_var.get().strip())
        for child in tree.get_children():
            tree.delete(child)
        rows.clear()
        mute_vars.clear()
        if not folder.is_dir():
            status_var.set(f"폴더 없음 — {folder}")
            return
        clips = list_folder_mp4s(folder)
        saved = load_mute_files()
        mute_map.clear()
        mute_map.update(saved)
        for i, path in enumerate(clips, 1):
            muted = mute_map.get(path.name.lower(), "sound") == "mute"
            label = "음소거" if muted else "소리"
            iid = tree.insert(
                "",
                tk.END,
                values=(i, path.name, label),
            )
            rows[iid] = path
            mute_vars[iid] = tk.StringVar(value=label)
        muted_n = sum(1 for v in mute_vars.values() if v.get() == "음소거")
        status_var.set(f"{len(clips)}개 · 음소거 {muted_n} — {folder}")
        persist()

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
    bind_path_entry_dnd(
        folder_ent,
        folder_var,
        mode="dir",
        on_set=lambda _p: (persist(), refresh_list()),
    )
    bind_path_row_dnd(
        folder_ent,
        path_fr,
        folder_var,
        mode="dir",
        on_set=lambda _p: (persist(), refresh_list()),
    )

    def open_folder() -> None:
        folder = Path(folder_var.get().strip())
        if not folder.is_dir():
            safe_messagebox(root, "showwarning", "7_4 mp4Merge", f"폴더가 없습니다.\n{folder}")
            return
        if sys.platform == "win32":
            os.startfile(str(folder))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(folder)])

    def open_log() -> None:
        folder = Path(folder_var.get().strip())
        candidates = [
            folder / MERGE_LOG_NAME,
            Path(log_file_display()),
        ]
        for p in candidates:
            if p.is_file():
                if sys.platform == "win32":
                    os.startfile(str(p))  # type: ignore[attr-defined]
                else:
                    subprocess.Popen(["xdg-open", str(p)])
                return
        safe_messagebox(
            root,
            "showinfo",
            "7_4 mp4Merge",
            f"로그 파일이 아직 없습니다.\n\n{log_file_display()}",
        )

    btn_refresh = ttk.Button(btn_fr, text="목록 새로고침", command=refresh_list)
    btn_refresh.pack(side=tk.LEFT)
    ttk.Button(btn_fr, text="폴더 열기", command=open_folder).pack(side=tk.LEFT, padx=(8, 0))
    ttk.Button(btn_fr, text="로그 열기", command=open_log).pack(side=tk.LEFT, padx=(8, 0))

    mid = ttk.Frame(top)
    mid.grid(row=2, column=0, sticky="nsew", pady=(4, 0))
    mid.columnconfigure(0, weight=1)
    mid.rowconfigure(0, weight=3)
    mid.rowconfigure(1, weight=2)

    tree_fr = ttk.Frame(mid)
    tree_fr.grid(row=0, column=0, sticky="nsew")
    tree_fr.columnconfigure(0, weight=1)
    tree_fr.rowconfigure(0, weight=1)
    cols = ("no", "name", "mute")
    tree = ttk.Treeview(tree_fr, columns=cols, show="headings", selectmode="browse")
    tree.heading("no", text="#")
    tree.heading("name", text="파일")
    tree.heading("mute", text="음소거")
    tree.column("no", width=48, anchor=tk.CENTER, stretch=False)
    tree.column("name", width=420, anchor=tk.W)
    tree.column("mute", width=80, anchor=tk.CENTER, stretch=False)
    ys = ttk.Scrollbar(tree_fr, orient=tk.VERTICAL, command=tree.yview)
    tree.configure(yscrollcommand=ys.set)
    tree.grid(row=0, column=0, sticky="nsew")
    ys.grid(row=0, column=1, sticky="ns")

    def on_mute_click(event: tk.Event) -> None:
        if busy["v"]:
            return
        region = tree.identify_region(event.x, event.y)
        if region != "cell":
            return
        col = tree.identify_column(event.x)
        if col != "#3":
            return
        iid = tree.identify_row(event.y)
        if not iid or iid not in rows:
            return
        mv = mute_vars[iid]
        nxt = "소리" if mv.get() == "음소거" else "음소거"
        mv.set(nxt)
        vals = list(tree.item(iid, "values"))
        vals[2] = nxt
        tree.item(iid, values=vals)
        persist()
        muted_n = sum(1 for v in mute_vars.values() if v.get() == "음소거")
        status_var.set(f"{len(rows)}개 · 음소거 {muted_n} — {Path(folder_var.get()).name}")

    tree.bind("<Button-1>", on_mute_click)

    log_fr = ttk.LabelFrame(mid, text="병합 로그")
    log_fr.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
    log_fr.columnconfigure(0, weight=1)
    log_fr.rowconfigure(0, weight=1)
    log_txt = tk.Text(log_fr, height=8, wrap=tk.WORD, state=tk.DISABLED)
    log_ys = ttk.Scrollbar(log_fr, orient=tk.VERTICAL, command=log_txt.yview)
    log_txt.configure(yscrollcommand=log_ys.set)
    log_txt.grid(row=0, column=0, sticky="nsew")
    log_ys.grid(row=0, column=1, sticky="ns")

    def append_log(msg: str) -> None:
        log_txt.configure(state=tk.NORMAL)
        log_txt.insert(tk.END, msg.rstrip() + "\n")
        log_txt.see(tk.END)
        log_txt.configure(state=tk.DISABLED)

    def clear_log() -> None:
        log_txt.configure(state=tk.NORMAL)
        log_txt.delete("1.0", tk.END)
        log_txt.configure(state=tk.DISABLED)

    bot = ttk.Frame(top)
    bot.grid(row=3, column=0, sticky="ew", pady=(8, 0))
    bot.columnconfigure(0, weight=1)
    ttk.Progressbar(bot, variable=progress_var, maximum=100.0).grid(
        row=0, column=0, sticky="ew"
    )
    ttk.Label(bot, textvariable=progress_text_var).grid(row=1, column=0, sticky="w", pady=(2, 0))
    ttk.Label(bot, textvariable=status_var).grid(row=2, column=0, sticky="w", pady=(2, 0))

    act = ttk.Frame(top)
    act.grid(row=4, column=0, sticky="ew", pady=(8, 0))

    def set_busy(v: bool) -> None:
        busy["v"] = v
        st = ["disabled"] if v else ["!disabled"]
        for w in (btn_pick, btn_merge, btn_refresh):
            try:
                w.state(st)
            except tk.TclError:
                pass
        try:
            btn_stop.state(["!disabled"] if v else ["disabled"])
        except tk.TclError:
            pass

    def run_merge() -> None:
        if busy["v"]:
            return
        folder = Path(folder_var.get().strip())
        if not folder.is_dir():
            safe_messagebox(root, "showwarning", "7_4 mp4Merge", f"폴더가 없습니다.\n{folder}")
            return
        clips = [rows[iid] for iid in tree.get_children() if iid in rows]
        if not clips:
            refresh_list()
            clips = [rows[iid] for iid in tree.get_children() if iid in rows]
        if not clips:
            safe_messagebox(root, "showwarning", "7_4 mp4Merge", "병합할 MP4가 없습니다.")
            return
        mute_names = {
            rows[iid].name.lower()
            for iid in tree.get_children()
            if iid in mute_vars and mute_vars[iid].get() == "음소거"
        }
        preview = "\n".join(
            f"  · {p.name}" + (" (음소거)" if p.name.lower() in mute_names else "")
            for p in clips[:12]
        )
        if len(clips) > 12:
            preview += f"\n  … 외 {len(clips) - 12}개"
        mute_hint = f"\n음소거 {len(mute_names)}개" if mute_names else ""
        try:
            from tkinter import messagebox

            ok = messagebox.askyesno(
                "7_4 mp4Merge",
                f"{len(clips)}개 파일을 자연순서로 병합합니다.\n"
                f"→ {ALL_MP4_NAME}{mute_hint}\n\n"
                f"{preview}\n\n"
                f"규격이 다른 파일만 본편(다수) 규격으로 맞춘 뒤\n"
                f"무손실 copy로 이어붙입니다.\n"
                f"실패 시 로그: {MERGE_LOG_NAME}\n\n병합할까요?",
                parent=root,
            )
        except tk.TclError:
            ok = False
        if not ok:
            return

        cancel_event.clear()
        set_busy(True)
        progress_var.set(0.0)
        progress_text_var.set("병합 0%")
        status_var.set(f"병합 시작… {ALL_MP4_NAME}")
        clear_log()
        persist()

        def work() -> None:
            try:

                def on_prog(pct: float, label: str) -> None:
                    safe_after(
                        root,
                        lambda p=pct, lb=label: (
                            progress_var.set(p),
                            progress_text_var.set(lb),
                        ),
                    )

                def on_log(msg: str) -> None:
                    safe_after(root, lambda m=msg: append_log(m))

                out = merge_folder_to_all(
                    clips,
                    folder,
                    mute_names=mute_names,
                    cancel_event=cancel_event,
                    on_progress=on_prog,
                    on_log=on_log,
                )

                def done() -> None:
                    set_busy(False)
                    progress_var.set(100.0)
                    progress_text_var.set("완료 100%")
                    status_var.set(f"병합 완료 — {ALL_MP4_NAME}")
                    show_toast(
                        root,
                        f"병합 완료\n\n{out}\n\n로그: {folder / MERGE_LOG_NAME}",
                        title="7_4 mp4Merge · 완료",
                    )

                safe_after(root, done)
            except Exception as e:
                mp4_merge_log_exc("merge", e)

                def fail() -> None:
                    set_busy(False)
                    progress_var.set(0.0)
                    progress_text_var.set("실패")
                    status_var.set(f"병합 실패 — {e}")
                    append_log(f"[오류] {e}")
                    safe_messagebox(
                        root,
                        "showerror",
                        "7_4 mp4Merge",
                        f"병합 실패:\n{e}\n\n로그: {folder / MERGE_LOG_NAME}",
                    )

                safe_after(root, fail)

        threading.Thread(target=work, daemon=True).start()

    def stop_merge() -> None:
        cancel_event.set()
        status_var.set("중지 요청…")

    btn_merge = ttk.Button(act, text="병합 → all.mp4", command=run_merge)
    btn_merge.pack(side=tk.LEFT)
    btn_stop = ttk.Button(act, text="중지", command=stop_merge, state=tk.DISABLED)
    btn_stop.pack(side=tk.LEFT, padx=(8, 0))

    ttk.Label(
        act,
        text="음소거 열 클릭 · 규격 다른 클립만 재인코딩 후 무손실 이어붙이기",
        foreground="#555",
    ).pack(side=tk.LEFT, padx=(16, 0))

    bind_close(root, standalone, on_close=persist)
    refresh_list()
    run_mainloop(root, standalone)


if __name__ == "__main__":
    main()

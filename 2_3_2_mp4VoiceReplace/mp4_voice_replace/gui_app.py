# -*- coding: utf-8 -*-
"""2_3_2_mp4VoiceReplace GUI — MP4 묵음 + 줄별 MP3(SRT 시각)."""

from __future__ import annotations

import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, font as tkfont, ttk

from mp4_voice_replace import __version__
from mp4_voice_replace.ffmpeg_util import ffmpeg_bin
from mp4_voice_replace.lines_io import load_lines_json
from mp4_voice_replace.replace import replace_voice
from mp4_voice_replace.settings import (
    default_dest_for_mp4,
    default_output_dir,
    folder_dialog_initial,
    guess_mp3_dir,
    guess_srt_beside_mp4,
    load_gui_settings,
    save_gui_settings,
)
from mp4_voice_replace.srt_parse import parse_srt_cues
from mp4_voice_replace.text_match import format_match_summary, match_placements
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
    if not standalone and getattr(root, "_mp4_voice_replace_gui_built", False):
        return
    if not standalone:
        setattr(root, "_mp4_voice_replace_gui_built", True)

    apply_window_chrome(
        root,
        standalone,
        title=f"2_3_2 mp4VoiceReplace {__version__}",
        minsize=(700, 400),
        geometry="860x460",
    )
    fam, sz = _default_font()
    root.option_add("*Font", (fam, sz))

    cfg = load_gui_settings()
    mp4_var = tk.StringVar(value=cfg.get("mp4_path") or "")
    srt_var = tk.StringVar(value=cfg.get("srt_path") or "")
    mp3_var = tk.StringVar(value=cfg.get("mp3_dir") or "")
    out_var = tk.StringVar(value=cfg.get("output_path") or "")
    status_var = tk.StringVar(
        value="MP4 + SRT + mp3/lines.json → 원음 묵음 · 대사 텍스트로 SRT 매칭 배치"
    )
    prog_var = tk.DoubleVar(value=0.0)
    busy = {"v": False}

    frm = ttk.Frame(root, padding=10)
    frm.pack(fill=tk.BOTH, expand=True)
    frm.grid_columnconfigure(1, weight=1)

    def persist() -> None:
        save_gui_settings(
            mp4_path=mp4_var.get().strip(),
            srt_path=srt_var.get().strip(),
            mp3_dir=mp3_var.get().strip(),
            output_path=out_var.get().strip(),
        )

    def set_progress(pct: float, msg: str = "") -> None:
        prog_var.set(max(0.0, min(100.0, float(pct))))
        if msg:
            status_var.set(msg)

    def set_busy(v: bool) -> None:
        busy["v"] = v
        st = tk.DISABLED if v else tk.NORMAL
        for b in (btn_mp4, btn_srt, btn_mp3, btn_out, btn_check, btn_run):
            try:
                b.configure(state=st)
            except tk.TclError:
                pass

    def refresh_match_status() -> str:
        srt_p = Path(srt_var.get().strip()) if srt_var.get().strip() else None
        mp3_p = Path(mp3_var.get().strip()) if mp3_var.get().strip() else None
        try:
            if not srt_p or not srt_p.is_file():
                if mp3_p and (mp3_p / "lines.json").is_file():
                    n_lines = len(load_lines_json(mp3_p))
                    msg = f"lines.json {n_lines}줄 · SRT 미지정"
                else:
                    msg = "SRT · mp3 폴더를 지정하세요."
                status_var.set(msg)
                return msg
            if not mp3_p or not (mp3_p / "lines.json").is_file():
                n_cues = len(parse_srt_cues(srt_p))
                msg = f"SRT 큐 {n_cues}개 · lines.json 미지정"
                status_var.set(msg)
                return msg
            cues = parse_srt_cues(srt_p)
            lines = load_lines_json(mp3_p)
            placements = match_placements(cues, lines)
            msg = format_match_summary(placements, n_cues=len(cues))
            status_var.set(msg.split("\n")[0])
            return msg
        except Exception as e:
            msg = f"매칭 실패: {e}"
            status_var.set(msg.split("\n")[0] if msg else str(e))
            return msg

    def apply_mp4_guesses(path: str) -> None:
        p = Path(path)
        if not p.is_file():
            return
        if not srt_var.get().strip():
            g = guess_srt_beside_mp4(p)
            if g:
                srt_var.set(str(g))
        if not mp3_var.get().strip():
            g3 = guess_mp3_dir(p)
            if g3:
                mp3_var.set(str(g3))
        if not out_var.get().strip():
            out_var.set(str(default_dest_for_mp4(p, default_output_dir())))
        refresh_match_status()

    ttk.Label(frm, text="MP4", width=12).grid(row=0, column=0, sticky="w")
    mp4_ent = ttk.Entry(frm, textvariable=mp4_var)
    mp4_ent.grid(row=0, column=1, sticky="ew", padx=4)

    def pick_mp4() -> None:
        init = folder_dialog_initial(
            Path(mp4_var.get()).parent if mp4_var.get().strip() else None
        )
        p = filedialog.askopenfilename(
            parent=root,
            title="MP4 영상",
            initialdir=init,
            filetypes=[("MP4", "*.mp4"), ("Video", "*.mp4;*.mkv;*.mov"), ("All", "*.*")],
        )
        if p:
            mp4_var.set(p)
            touch_workspace_from_path(p)
            apply_mp4_guesses(p)
            persist()

    def on_mp4_drop(path: str) -> None:
        p = Path(path)
        if p.is_file():
            mp4_var.set(str(p))
            touch_workspace_from_path(str(p))
            apply_mp4_guesses(str(p))
            persist()

    btn_mp4 = ttk.Button(frm, text="찾기", command=pick_mp4, width=8)
    btn_mp4.grid(row=0, column=2, padx=(4, 0))
    bind_path_row_dnd(mp4_ent, frm, mp4_var, mode="file", on_set=on_mp4_drop)
    bind_path_entry_dnd(mp4_ent, mp4_var, mode="file", on_set=on_mp4_drop)

    ttk.Label(frm, text="SRT", width=12).grid(row=1, column=0, sticky="w", pady=(6, 0))
    srt_ent = ttk.Entry(frm, textvariable=srt_var)
    srt_ent.grid(row=1, column=1, sticky="ew", padx=4, pady=(6, 0))

    def pick_srt() -> None:
        init = folder_dialog_initial(
            Path(srt_var.get()).parent if srt_var.get().strip() else None
        )
        p = filedialog.askopenfilename(
            parent=root,
            title="SRT (대사 텍스트 매칭)",
            initialdir=init,
            filetypes=[("SRT", "*.srt"), ("All", "*.*")],
        )
        if p:
            srt_var.set(p)
            touch_workspace_from_path(p)
            persist()
            refresh_match_status()

    btn_srt = ttk.Button(frm, text="찾기", command=pick_srt, width=8)
    btn_srt.grid(row=1, column=2, padx=(4, 0), pady=(6, 0))
    bind_path_entry_dnd(srt_ent, srt_var, mode="file", on_set=lambda _p: refresh_match_status())

    ttk.Label(frm, text="mp3 폴더", width=12).grid(row=2, column=0, sticky="w", pady=(6, 0))
    mp3_ent = ttk.Entry(frm, textvariable=mp3_var)
    mp3_ent.grid(row=2, column=1, sticky="ew", padx=4, pady=(6, 0))

    def pick_mp3() -> None:
        init = folder_dialog_initial(
            Path(mp3_var.get().strip()) if mp3_var.get().strip() else None
        )
        d = filedialog.askdirectory(
            parent=root, title="mp3 폴더 (lines.json + 01.mp3…)", initialdir=init
        )
        if d:
            mp3_var.set(d)
            touch_workspace_from_path(d)
            persist()
            refresh_match_status()

    btn_mp3 = ttk.Button(frm, text="찾기", command=pick_mp3, width=8)
    btn_mp3.grid(row=2, column=2, padx=(4, 0), pady=(6, 0))
    bind_path_row_dnd(
        mp3_ent, frm, mp3_var, mode="dir", on_set=lambda _p: refresh_match_status()
    )
    bind_path_entry_dnd(
        mp3_ent, mp3_var, mode="dir", on_set=lambda _p: refresh_match_status()
    )

    ttk.Label(frm, text="출력 MP4", width=12).grid(row=3, column=0, sticky="w", pady=(6, 0))
    out_ent = ttk.Entry(frm, textvariable=out_var)
    out_ent.grid(row=3, column=1, sticky="ew", padx=4, pady=(6, 0))

    def pick_out() -> None:
        init = folder_dialog_initial(
            Path(out_var.get()).parent if out_var.get().strip() else None
        )
        p = filedialog.asksaveasfilename(
            parent=root,
            title="출력 MP4",
            initialdir=init,
            defaultextension=".mp4",
            filetypes=[("MP4", "*.mp4"), ("All", "*.*")],
        )
        if p:
            out_var.set(p)
            persist()

    btn_out = ttk.Button(frm, text="찾기", command=pick_out, width=8)
    btn_out.grid(row=3, column=2, padx=(4, 0), pady=(6, 0))

    tip = ttk.Label(
        frm,
        text="원음 제거 후 lines.json 대사를 SRT 텍스트에 매칭해 시작 시각에 MP3를 배치합니다. (합쳐진/쪼개진 큐 지원)",
        foreground="#555",
    )
    tip.grid(row=4, column=0, columnspan=3, sticky="w", pady=(10, 0))

    act = ttk.Frame(frm)
    act.grid(row=5, column=0, columnspan=3, sticky="w", pady=(14, 0))

    def do_check() -> None:
        msg = refresh_match_status()
        safe_messagebox(root, "showinfo", "2_3_2 mp4VoiceReplace", msg)

    def do_run() -> None:
        if busy["v"]:
            return
        video = Path(mp4_var.get().strip())
        srt = Path(srt_var.get().strip())
        mp3 = Path(mp3_var.get().strip())
        dest = Path(out_var.get().strip()) if out_var.get().strip() else default_dest_for_mp4(
            video, default_output_dir()
        )
        if not video.is_file():
            safe_messagebox(root, "showwarning", "2_3_2 mp4VoiceReplace", "MP4를 선택하세요.")
            return
        if not srt.is_file():
            safe_messagebox(root, "showwarning", "2_3_2 mp4VoiceReplace", "SRT를 선택하세요.")
            return
        if not (mp3 / "lines.json").is_file():
            safe_messagebox(
                root,
                "showwarning",
                "2_3_2 mp4VoiceReplace",
                "mp3 폴더에 lines.json 이 필요합니다.",
            )
            return
        if not ffmpeg_bin():
            safe_messagebox(
                root,
                "showerror",
                "2_3_2 mp4VoiceReplace",
                "ffmpeg 가 필요합니다 (PATH 또는 tools/ffmpeg).",
            )
            return
        out_var.set(str(dest))
        persist()

        def work() -> None:
            try:
                result = replace_voice(
                    mp4_path=video,
                    srt_path=srt,
                    mp3_dir=mp3,
                    dest_path=dest,
                    on_progress=lambda m, p: safe_after(
                        root, lambda msg=m, pct=p: set_progress(pct, msg)
                    ),
                )

                def done() -> None:
                    set_busy(False)
                    set_progress(100.0, f"완료 → {result}")
                    show_toast(
                        root,
                        f"저장\n{result}",
                        title="2_3_2 mp4VoiceReplace · 완료",
                    )

                safe_after(root, done)
            except Exception as e:
                err = str(e)

                def fail() -> None:
                    set_busy(False)
                    set_progress(0.0, f"오류: {err}")
                    safe_messagebox(root, "showerror", "2_3_2 mp4VoiceReplace", err)

                safe_after(root, fail)

        set_busy(True)
        set_progress(0.0, "음성 교체 시작…")
        threading.Thread(target=work, daemon=True).start()

    btn_check = ttk.Button(act, text="대사 매칭 확인", command=do_check)
    btn_check.pack(side=tk.LEFT, padx=(0, 8))
    btn_run = ttk.Button(act, text="음성 교체 실행", command=do_run)
    btn_run.pack(side=tk.LEFT)

    prog_fr = ttk.Frame(frm)
    prog_fr.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(12, 0))
    prog_fr.grid_columnconfigure(0, weight=1)
    ttk.Progressbar(prog_fr, maximum=100, mode="determinate", variable=prog_var).grid(
        row=0, column=0, sticky="ew"
    )
    ttk.Label(prog_fr, textvariable=status_var).grid(
        row=1, column=0, sticky="ew", pady=(4, 0)
    )

    def on_close() -> None:
        persist()

    if standalone:
        bind_close(root, standalone, on_close)
    else:
        bind_hub_destroy(root, on_close)

    if mp4_var.get().strip():
        root.after(80, lambda: apply_mp4_guesses(mp4_var.get().strip()))
    else:
        root.after(80, refresh_match_status)

    run_mainloop(root, standalone)

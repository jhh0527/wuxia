# -*- coding: utf-8 -*-
"""MP4 원음 제거 + SRT(대사 매칭) 시각에 줄별 MP3 배치."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from mp4_voice_replace.ffmpeg_util import ffmpeg_bin, probe_duration_sec, run_ffmpeg
from mp4_voice_replace.lines_io import load_lines_json
from mp4_voice_replace.srt_parse import parse_srt_cues
from mp4_voice_replace.text_match import Placement, format_match_summary, match_placements

ProgressCb = Callable[[str, float], None]


def replace_voice(
    *,
    mp4_path: Path | str,
    srt_path: Path | str,
    mp3_dir: Path | str,
    dest_path: Path | str,
    on_progress: ProgressCb | None = None,
) -> Path:
    """원음 묵음 영상 + 줄별 MP3(대사↔SRT 매칭 시각) → dest mp4."""
    video = Path(mp4_path)
    srt = Path(srt_path)
    out = Path(dest_path)
    if not video.is_file():
        raise FileNotFoundError(f"MP4 없음: {video}")
    if not srt.is_file():
        raise FileNotFoundError(f"SRT 없음: {srt}")

    ff = ffmpeg_bin()
    if not ff:
        raise RuntimeError("ffmpeg 가 PATH(또는 tools/ffmpeg)에 없습니다.")

    if on_progress:
        on_progress("SRT · lines.json 로드·매칭…", 5.0)

    cues = parse_srt_cues(srt)
    lines = load_lines_json(mp3_dir)
    placements = match_placements(cues, lines)

    if on_progress:
        on_progress(format_match_summary(placements, n_cues=len(cues)).split("\n")[0], 12.0)

    vid_dur = probe_duration_sec(video)
    if not vid_dur or vid_dur <= 0.05:
        raise RuntimeError(f"영상 길이를 알 수 없습니다: {video}")

    return _mux_placements(
        video=video,
        placements=placements,
        dest=out,
        vid_dur=vid_dur,
        ff=ff,
        on_progress=on_progress,
    )


def _mux_placements(
    *,
    video: Path,
    placements: list[Placement],
    dest: Path,
    vid_dur: float,
    ff: Path,
    on_progress: ProgressCb | None,
) -> Path:
    n = len(placements)
    fc_parts: list[str] = []
    labels: list[str] = []
    for i, pl in enumerate(placements):
        delay = max(0, int(pl.start_ms))
        lab = f"a{i}"
        fc_parts.append(
            f"[{i + 1}:a]"
            f"aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
            f"adelay={delay}|{delay},"
            f"asetpts=PTS-STARTPTS"
            f"[{lab}]"
        )
        labels.append(f"[{lab}]")

    mix_in = "".join(labels)
    if n == 1:
        fc_parts.append(
            f"{labels[0]}apad=whole_dur={vid_dur:.3f},atrim=0:{vid_dur:.3f},asetpts=PTS-STARTPTS[aout]"
        )
    else:
        fc_parts.append(
            f"{mix_in}amix=inputs={n}:duration=longest:dropout_transition=0:normalize=0[amixed]"
        )
        fc_parts.append(
            f"[amixed]apad=whole_dur={vid_dur:.3f},atrim=0:{vid_dur:.3f},asetpts=PTS-STARTPTS[aout]"
        )
    filter_complex = ";".join(fc_parts)

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp.mp4")
    if tmp.is_file():
        tmp.unlink()

    if on_progress:
        on_progress(f"ffmpeg 합성 ({n}줄)…", 20.0)

    def build_cmd(*, reencode: bool) -> list[str]:
        cmd: list[str] = [str(ff), "-y", "-i", str(video)]
        for pl in placements:
            cmd += ["-i", str(pl.line.path)]
        vcodec = (
            ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18"]
            if reencode
            else ["-c:v", "copy"]
        )
        cmd += [
            "-filter_complex",
            filter_complex,
            "-map",
            "0:v:0",
            "-map",
            "[aout]",
            *vcodec,
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-t",
            f"{vid_dur:.3f}",
            "-movflags",
            "+faststart",
            str(tmp),
        ]
        return cmd

    try:
        run_ffmpeg(build_cmd(reencode=False), timeout=max(600.0, vid_dur * 30))
    except RuntimeError:
        if on_progress:
            on_progress("영상 재인코딩으로 재시도…", 50.0)
        if tmp.is_file():
            tmp.unlink(missing_ok=True)
        run_ffmpeg(build_cmd(reencode=True), timeout=max(900.0, vid_dur * 60))

    if dest.is_file():
        dest.unlink()
    tmp.replace(dest)

    if on_progress:
        on_progress(f"완료 → {dest.name}", 100.0)
    return dest

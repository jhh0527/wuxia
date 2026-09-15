# -*- coding: utf-8 -*-
"""MP4 폴더 자산 — SRT 번호(시작 초) 타임라인 합성."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from mp4_search.naming import (
    ALL_MP4_NAME,
    is_thumbnail_asset,
    parse_srt_asset_number,
    scan_srt_assets,
    timeline_asset_number,
)


TITLE_CARD_ASSET_KEY = 0
TITLE_CARD_DURATION_SEC = 3.0
OPENING_BRIDGE_ASSET_KEY = 5


def infer_scene_interval_from_png_map(png_map: dict[int, Path]) -> int:
    """PNG 파일 번호 간격 — ``SRT_000``·``SRT_005``·썸네일 제외."""
    keys = sorted(
        int(k)
        for k in png_map
        if int(k) > TITLE_CARD_ASSET_KEY
        and int(k) != OPENING_BRIDGE_ASSET_KEY
        and not is_thumbnail_asset(int(k))
    )
    if len(keys) >= 2:
        return max(1, keys[1] - keys[0])
    if keys:
        return max(20, int(keys[0]))
    return 20


def apply_title_card_asset_times(
    png_map: dict[int, Path],
    asset_start_times: dict[int, float] | None = None,
    *,
    title_duration: float = TITLE_CARD_DURATION_SEC,
    interval_sec: int | None = None,
) -> dict[int, float]:
    """``SRT_000`` 제목 카드 0초 · ``SRT_005``(5초) 또는 ``SRT_020``(3초) 첫 본문."""
    out = dict(asset_start_times or {})
    if TITLE_CARD_ASSET_KEY not in png_map:
        return out
    out[TITLE_CARD_ASSET_KEY] = 0.0
    if OPENING_BRIDGE_ASSET_KEY in png_map:
        out[OPENING_BRIDGE_ASSET_KEY] = float(OPENING_BRIDGE_ASSET_KEY)
        return out
    gap = max(1, int(interval_sec or infer_scene_interval_from_png_map(png_map)))
    scene_keys = [
        int(k)
        for k in sorted(png_map.keys())
        if int(k) > TITLE_CARD_ASSET_KEY
        and int(k) != OPENING_BRIDGE_ASSET_KEY
        and not is_thumbnail_asset(int(k))
    ]
    if not scene_keys:
        return out
    first_scene = int(scene_keys[0])
    if first_scene != gap:
        return out
    out[first_scene] = max(0.0, float(title_duration))
    return out


@dataclass(frozen=True)
class TimelineComposeJob:
    """한 타임라인 구간 합성 작업."""

    srt_id: int
    mark_sec: float
    video: Path | None
    image: Path | None
    duration_sec: float
    video_from: int
    image_from: int | None
    video_start_sec: float = 0.0
    image_effect: str = "fixed"
    is_gap: bool = False
    is_hold: bool = False
    is_image_only: bool = False
    video_loop: bool = True


def merge_asset_maps(
    mp4_map: dict[int, Path],
    png_map: dict[int, Path],
    *,
    extra_mp4: dict[int, Path] | None = None,
    extra_png: dict[int, Path] | None = None,
) -> tuple[dict[int, Path], dict[int, Path]]:
    """폴더 스캔 + GUI 자산 병합 — 키는 **파일명** ``SRT_NNN`` 번호만 사용."""
    mp4 = dict(mp4_map)
    png = dict(png_map)

    def _merge(extra: dict[int, Path] | None, dest: dict[int, Path]) -> None:
        if not extra:
            return
        for _k, p in extra.items():
            p = Path(p)
            if not p.is_file():
                continue
            num = parse_srt_asset_number(p.name)
            if num is not None:
                dest[int(num)] = p

    _merge(extra_mp4, mp4)
    _merge(extra_png, png)
    # SRT_9999 썸네일은 합성 맵에서 제외
    mp4 = {k: v for k, v in mp4.items() if not is_thumbnail_asset(k)}
    png = {k: v for k, v in png.items() if not is_thumbnail_asset(k)}
    return mp4, png


def pick_asset_at_timeline(asset_map: dict[int, Path], timeline_sec: int) -> tuple[int, Path] | None:
    """파일 번호 ≤ ``timeline_sec`` 인 자산 중 **가장 큰** 번호 (4_1_video 이미지 매칭과 동일).

    ``SRT_9999`` 썸네일은 후보에서 제외.
    """
    if not asset_map:
        return None
    t = max(0, int(timeline_sec))
    leq = [n for n in asset_map if n <= t and not is_thumbnail_asset(n)]
    if not leq:
        return None
    key = max(leq)
    return key, asset_map[key]


def asset_timeline_mark(asset_key: int, asset_start_times: dict[int, float] | None) -> float:
    """``SRT_NNN`` 파일 번호 → SRT 타임스탬프 시작(초). 없으면 번호(정수) 그대로."""
    if asset_start_times and asset_key in asset_start_times:
        return max(0.0, float(asset_start_times[asset_key]))
    return float(asset_key)


def build_asset_start_times_from_srt(srt_path: Path) -> dict[int, float]:
    """SRT 파일 → ``SRT_NNN`` 번호별 가장 이른 시작(초)."""
    from mp4_search.srt_parse import parse_srt_cues_timed

    starts: dict[int, float] = {}
    srt_path = Path(srt_path)
    if not srt_path.is_file():
        return starts
    for _sid, _text, st_ms, _en_ms in parse_srt_cues_timed(srt_path):
        t = st_ms / 1000.0
        key = timeline_asset_number(t)
        if key not in starts or t < starts[key]:
            starts[key] = t
    return starts


def folder_asset_display_owners(
    asset_map: dict[int, Path],
    cues: list[tuple[int, str, int, int]],
    asset_start_times: dict[int, float] | None = None,
) -> dict[int, int]:
    """폴더 ``SRT_NNN`` 자산 → 그리드에 표시할 SRT map_id (자산 1개 = 줄 1개).

    파일 번호 N의 시작 시각은 ``asset_start_times[N]`` (없으면 N초)이며,
    그 시각이 속한 자막 줄(시작이 그 시각 이하인 마지막 줄)에 MP4/PNG 파일명을 표시한다.
    자막 중간에서 시작하는 추가 컷(``SRT_296`` 등)도 다음 줄의 격자 자산(``SRT_300``)에
    밀리지 않고 자기 구간 줄에 붙는다. 한 줄에 자산이 겹치면 번호가 작은 쪽이 그 줄을
    쓰고 나머지는 다음 빈 줄로 밀린다. 첫 자막보다 앞이면 첫 줄에 붙인다.
    ``SRT_9999`` 썸네일은 제외.
    """
    owners: dict[int, int] = {}
    sorted_cues = sorted(cues, key=lambda c: c[2])
    if not sorted_cues:
        return owners
    starts = [c[2] / 1000.0 for c in sorted_cues]
    taken: set[int] = set()
    for fk in sorted(k for k in asset_map if not is_thumbnail_asset(k)):
        mark = asset_timeline_mark(fk, asset_start_times)
        idx = 0
        for i, st in enumerate(starts):
            if st > mark + 0.001:
                break
            # 같은 시각의 줄이 여러 개면 그중 첫 줄
            if i == 0 or st > starts[i - 1] + 0.001:
                idx = i
        slot = idx
        while slot in taken:
            slot += 1
        if slot >= len(sorted_cues):
            # 뒤에 빈 줄이 없으면 자기 구간 줄 (표시 못 한 자산은 목록 끝 행으로)
            slot = idx
        else:
            taken.add(slot)
        owners[fk] = int(sorted_cues[slot][0])
    return owners


def folder_asset_for_cue_row(
    srt_id: int,
    asset_sec: int,
    asset_map: dict[int, Path],
    owners: dict[int, int],
) -> Path | None:
    """한 SRT 줄에 표시할 폴더 자산 (``folder_asset_display_owners`` 배정 기준).

    같은 줄에 여러 자산이 매칭되면 줄의 파일 번호(``asset_sec``)에 가장 가까운 것을 고른다.
    """
    candidates = [
        fk for fk, owner_sid in owners.items() if owner_sid == srt_id and fk in asset_map
    ]
    if not candidates:
        return None
    best = min(candidates, key=lambda fk: (abs(int(fk) - int(asset_sec)), int(fk)))
    return asset_map[best]


def unlisted_folder_asset_numbers(
    asset_mp4: dict[int, Path],
    asset_png: dict[int, Path],
    listed_numbers: set[int] | frozenset[int],
) -> list[int]:
    """목록 행에 아직 안 붙은 폴더 ``SRT_NNN`` 번호 (썸네일 제외, 오름차순)."""
    out: list[int] = []
    for n in sorted(set(asset_mp4) | set(asset_png)):
        if is_thumbnail_asset(n):
            continue
        if n in listed_numbers:
            continue
        out.append(int(n))
    return out


def missing_timeline_mp4_slots(
    mp4_map: dict[int, Path],
    asset_start_times: dict[int, float] | None,
    *,
    expected_slots: set[int] | frozenset[int] | None = None,
) -> list[int]:
    """MP4가 없는데 사용자가 지정한 슬롯 (합성 전 경고용).

    ``expected_slots`` 가 없으면 경고하지 않는다.
    자막만 있고 MP4·다운로드 지정이 없는 초(예: 26·28·30)는 누락으로 보지 않는다.
    """
    _ = asset_start_times
    if not expected_slots:
        return []
    missing: list[int] = []
    for k in sorted(int(x) for x in expected_slots):
        if k not in mp4_map:
            missing.append(k)
    return missing


def timeline_mark_asset_keys(
    mp4_map: dict[int, Path],
    png_map: dict[int, Path],
    asset_start_times: dict[int, float] | None,
) -> set[int]:
    """합성 타임라인 마크 — MP4 우선, MP4 없으면 이미지 파일 번호.

    ``SRT_9999``(썸네일)는 제외.
    """
    _ = asset_start_times
    keys = set(mp4_map.keys()) if mp4_map else set(png_map.keys())
    return {k for k in keys if not is_thumbnail_asset(k)}


def lookup_mark_schedule(schedule: dict[float, int], mark_sec: float) -> int | None:
    """``mark_sec`` 에 해당하는 자산 번호 (부동소수 오차 허용)."""
    if not schedule:
        return None
    if mark_sec in schedule:
        return schedule[mark_sec]
    for t, key in schedule.items():
        if abs(float(t) - float(mark_sec)) < 0.001:
            return key
    return None


def asset_mark_schedule(
    mp4_map: dict[int, Path],
    png_map: dict[int, Path],
    asset_start_times: dict[int, float] | None,
) -> dict[float, int]:
    """타임라인 시작 시각 → 자산 파일 번호."""
    schedule: dict[float, int] = {}
    for k in sorted(timeline_mark_asset_keys(mp4_map, png_map, asset_start_times)):
        schedule[asset_timeline_mark(k, asset_start_times)] = int(k)
    return schedule


def png_mark_schedule(
    png_map: dict[int, Path],
    asset_start_times: dict[int, float] | None,
) -> dict[float, int]:
    """타임라인 시작 시각 → PNG 파일 번호 (``SRT_9999`` 썸네일 제외)."""
    schedule: dict[float, int] = {}
    for k in sorted(png_map.keys()):
        if is_thumbnail_asset(k):
            continue
        schedule[asset_timeline_mark(k, asset_start_times)] = int(k)
    return schedule


def _compose_fallback_without_video(
    *,
    start: float,
    start_i: int,
    duration: float,
    png_map: dict[int, Path],
    png_schedule: dict[float, int],
    png_effects: dict[int, str] | None,
    last_video: Path | None,
    last_vid_key: int | None,
) -> TimelineComposeJob:
    """MP4 없는 구간 — 이전 MP4 연장, PNG 단독(첫 MP4 이전 등), 또는 빈 구간."""
    if last_video and last_video.is_file():
        return TimelineComposeJob(
            srt_id=start_i,
            mark_sec=start,
            video=last_video,
            image=None,
            duration_sec=duration,
            video_from=last_vid_key or 0,
            image_from=None,
            is_hold=True,
        )
    img_key = lookup_mark_schedule(png_schedule, start)
    if img_key is not None and img_key in png_map:
        img_effect = "fixed"
        if png_effects:
            img_effect = png_effects.get(int(img_key), "fixed")
        return TimelineComposeJob(
            srt_id=start_i,
            mark_sec=start,
            video=None,
            image=png_map[img_key],
            duration_sec=duration,
            video_from=0,
            image_from=img_key,
            image_effect=img_effect,
            is_image_only=True,
        )
    return TimelineComposeJob(
        srt_id=start_i,
        mark_sec=start,
        video=None,
        image=None,
        duration_sec=duration,
        video_from=0,
        image_from=None,
        is_gap=True,
    )


def next_timeline_mark_after(
    mp4_map: dict[int, Path],
    png_map: dict[int, Path],
    after_sec: float,
    *,
    asset_start_times: dict[int, float] | None = None,
) -> float | None:
    """``after_sec`` 보다 큰 다음 MP4·PNG 시작 시각(초)."""
    marks = [
        asset_timeline_mark(k, asset_start_times)
        for k in timeline_mark_asset_keys(mp4_map, png_map, asset_start_times)
    ]
    later = [m for m in marks if m > float(after_sec) + 0.001]
    return min(later) if later else None


def timeline_total_sec(
    segments: list[tuple[float, float]],
    *,
    cue_end_times: list[float] | None = None,
) -> float:
    """SRT 구간 기준 타임라인 끝(초)."""
    ends: list[float] = []
    if segments:
        ends.append(max(float(s) + max(0.0, float(dur)) for s, dur in segments))
    if cue_end_times:
        ends.extend(float(t) for t in cue_end_times if t > 0)
    return max(ends) if ends else 0.0


def timeline_end_sec(
    segments: list[tuple[float, float]],
    mp4_map: dict[int, Path],
    png_map: dict[int, Path],
    *,
    cue_end_times: list[float] | None = None,
    audio_sec: float | None = None,
    asset_start_times: dict[int, float] | None = None,
) -> float:
    """합성 타임라인 전체 길이(초)."""
    seg_end = timeline_total_sec(segments, cue_end_times=cue_end_times)
    keys = sorted(timeline_mark_asset_keys(mp4_map, png_map, asset_start_times))
    if seg_end <= 0 and not keys:
        if audio_sec and audio_sec > 0:
            return float(audio_sec)
        return 0.0
    end = seg_end
    if keys:
        asset_mark = max(keys)
        last_t = asset_timeline_mark(asset_mark, asset_start_times)
        nxt = next_timeline_mark_after(
            mp4_map,
            png_map,
            last_t,
            asset_start_times=asset_start_times,
        )
        if nxt:
            end = max(end, nxt)
        else:
            end = max(end, last_t + 3.0)
        end = max(end, last_t + 1.0)
    if audio_sec and audio_sec > 0:
        end = max(end, float(audio_sec))
    return end


def clip_duration_until_next_asset(
    asset_key: int,
    mp4_map: dict[int, Path],
    png_map: dict[int, Path],
    *,
    total_end: float | None = None,
    asset_start_times: dict[int, float] | None = None,
) -> float | None:
    """적용 종료초 미지정 시 — 다음 영상/이미지 시작까지 길이(초)."""
    start_t = asset_timeline_mark(asset_key, asset_start_times)
    nxt = next_timeline_mark_after(
        mp4_map,
        png_map,
        start_t,
        asset_start_times=asset_start_times,
    )
    if nxt is not None:
        return max(0.1, float(nxt - start_t))
    if total_end is not None and total_end > start_t:
        return max(0.1, float(total_end - start_t))
    return None


def pick_image_for_segment(
    mp4_map: dict[int, Path],
    png_map: dict[int, Path],
    mark_sec: float,
    vid_key: int,
) -> tuple[int, Path] | None:
    """PNG 오버레이 — ``mark_sec`` 시점에 해당 MP4 구간 위에 표시할 PNG."""
    hit_i = pick_asset_at_timeline(png_map, int(mark_sec))
    if not hit_i:
        return None
    img_key, image = hit_i
    v_at_img = pick_asset_at_timeline(mp4_map, img_key)
    if not v_at_img or v_at_img[0] != vid_key:
        return None
    return img_key, image


def png_overlay_mark_times(
    png_map: dict[int, Path],
    asset_start_times: dict[int, float] | None,
) -> set[float]:
    """PNG 시작 시각 — MP4와 번호가 달라도 구간 분할용 (썸네일 제외)."""
    return {
        asset_timeline_mark(k, asset_start_times)
        for k in png_map
        if not is_thumbnail_asset(k)
    }


def _list_image_only_compose_jobs(
    png_map: dict[int, Path],
    segments: list[tuple[float, float]],
    *,
    cue_end_times: list[float] | None = None,
    audio_sec: float | None = None,
    png_effects: dict[int, str] | None = None,
    asset_start_times: dict[int, float] | None = None,
) -> list[TimelineComposeJob]:
    """MP4 없이 ``SRT_NNN`` 이미지만으로 타임라인 클립 생성."""
    if not png_map:
        return []

    total_end = timeline_end_sec(
        segments,
        {},
        png_map,
        cue_end_times=cue_end_times,
        audio_sec=audio_sec,
        asset_start_times=asset_start_times,
    )
    if total_end <= 0.05:
        return []

    mark_schedule: dict[float, int] = {}
    for k in sorted(png_map.keys()):
        if is_thumbnail_asset(k):
            continue
        mark_schedule[asset_timeline_mark(k, asset_start_times)] = int(k)

    marks = sorted({0.0, float(total_end), *mark_schedule.keys()})
    jobs: list[TimelineComposeJob] = []
    for i, start in enumerate(marks[:-1]):
        end = min(marks[i + 1], total_end)
        duration = end - start
        if duration <= 0.05:
            continue
        switch_key = lookup_mark_schedule(mark_schedule, start)
        start_i = switch_key if switch_key is not None else int(start)

        if switch_key is not None and switch_key in png_map and not is_thumbnail_asset(switch_key):
            img_key, image = switch_key, png_map[switch_key]
        else:
            hit_i = pick_asset_at_timeline(png_map, start_i)
            if not hit_i:
                jobs.append(
                    TimelineComposeJob(
                        srt_id=start_i,
                        mark_sec=start,
                        video=None,
                        image=None,
                        duration_sec=duration,
                        video_from=0,
                        image_from=None,
                        is_gap=True,
                    )
                )
                continue
            img_key, image = hit_i

        img_effect = "fixed"
        if png_effects:
            img_effect = png_effects.get(int(img_key), "fixed")

        jobs.append(
            TimelineComposeJob(
                srt_id=start_i,
                mark_sec=start,
                video=None,
                image=image,
                duration_sec=duration,
                video_from=0,
                image_from=img_key,
                image_effect=img_effect,
                is_image_only=True,
            )
        )
    return jobs


def list_timeline_compose_jobs(
    folder: Path,
    segments: list[tuple[float, float]],
    *,
    cue_end_times: list[float] | None = None,
    audio_sec: float | None = None,
    extra_mp4: dict[int, Path] | None = None,
    extra_png: dict[int, Path] | None = None,
    png_effects: dict[int, str] | None = None,
    mp4_play_modes: dict[int, str] | None = None,
    asset_start_times: dict[int, float] | None = None,
) -> list[TimelineComposeJob]:
    """타임라인 자산 변경 지점마다 하나의 클립 — 다음 MP4·PNG 전까지 유지.

    - ``SRT_NNN`` 번호 = 파일명, 시작 시각 = SRT 타임스탬프(``asset_start_times``)
    - 구간 길이 = 다음 자산 시작(또는 SRT 끝) − 시작
    - ``mp4_play_modes``: 파일 번호별 반복(``loop``) / 마지막 장면 유지(``hold``)
    """
    from mp4_search.mp4_play_modes import MP4_MODE_LOOP, normalize_mp4_play_mode

    def _video_loop_for(key: int) -> bool:
        return (
            normalize_mp4_play_mode(
                (mp4_play_modes or {}).get(int(key), MP4_MODE_LOOP)
            )
            == MP4_MODE_LOOP
        )
    mp4_map, png_map = merge_asset_maps(
        *scan_srt_assets(folder),
        extra_mp4=extra_mp4,
        extra_png=extra_png,
    )
    asset_start_times = apply_title_card_asset_times(png_map, asset_start_times)
    if not mp4_map:
        if png_map:
            return _list_image_only_compose_jobs(
                png_map,
                segments,
                cue_end_times=cue_end_times,
                audio_sec=audio_sec,
                png_effects=png_effects,
                asset_start_times=asset_start_times,
            )
        return []

    total_end = timeline_end_sec(
        segments,
        mp4_map,
        png_map,
        cue_end_times=cue_end_times,
        audio_sec=audio_sec,
        asset_start_times=asset_start_times,
    )
    mark_schedule = asset_mark_schedule(mp4_map, png_map, asset_start_times)
    png_schedule = png_mark_schedule(png_map, asset_start_times)
    # 마크는 본편(SRT/MP3) 길이까지만 — 썸네일·미래 번호로 타임라인을 늘리지 않음
    if mark_schedule and total_end > 0.05:
        in_range = [t for t in mark_schedule if t <= total_end + 0.001]
        if in_range:
            total_end = max(total_end, max(in_range) + 1.0)
    elif mark_schedule:
        total_end = max(mark_schedule.keys()) + 1.0
    if total_end <= 0.05:
        return []

    marks = sorted(
        {
            0.0,
            float(total_end),
            *mark_schedule.keys(),
            *png_overlay_mark_times(png_map, asset_start_times),
        }
    )
    jobs: list[TimelineComposeJob] = []
    prev_vid_key: int | None = None
    prev_vid_offset = 0.0
    last_video: Path | None = None
    last_vid_key: int | None = None

    for i, start in enumerate(marks[:-1]):
        end = min(marks[i + 1], total_end)
        duration = end - start
        if duration <= 0.05:
            continue
        switch_key = lookup_mark_schedule(mark_schedule, start)
        start_i = switch_key if switch_key is not None else int(start)

        if switch_key is not None and switch_key in mp4_map:
            vid_key, video = switch_key, mp4_map[switch_key]
            video_start = 0.0
        elif switch_key is not None:
            jobs.append(
                _compose_fallback_without_video(
                    start=start,
                    start_i=start_i,
                    duration=duration,
                    png_map=png_map,
                    png_schedule=png_schedule,
                    png_effects=png_effects,
                    last_video=last_video,
                    last_vid_key=last_vid_key,
                )
            )
            continue
        else:
            hit_v = pick_asset_at_timeline(mp4_map, start_i)
            if not hit_v:
                jobs.append(
                    _compose_fallback_without_video(
                        start=start,
                        start_i=start_i,
                        duration=duration,
                        png_map=png_map,
                        png_schedule=png_schedule,
                        png_effects=png_effects,
                        last_video=last_video,
                        last_vid_key=last_vid_key,
                    )
                )
                continue
            vid_key, video = hit_v
            if vid_key == prev_vid_key:
                video_start = prev_vid_offset
            else:
                video_start = 0.0

        hit_i = pick_image_for_segment(mp4_map, png_map, start, vid_key)
        image = hit_i[1] if hit_i else None
        img_key = hit_i[0] if hit_i else None
        img_effect = "fixed"
        if img_key is not None and png_effects:
            img_effect = png_effects.get(int(img_key), "fixed")

        jobs.append(
            TimelineComposeJob(
                srt_id=start_i,
                mark_sec=start,
                video=video,
                image=image,
                duration_sec=duration,
                video_from=vid_key,
                image_from=img_key,
                video_start_sec=video_start,
                image_effect=img_effect,
                video_loop=_video_loop_for(vid_key),
            )
        )
        prev_vid_key = vid_key
        prev_vid_offset = video_start + duration
        last_video = video
        last_vid_key = vid_key
    return jobs


def _same_compose_image_path(a: Path | None, b: Path | None) -> bool:
    if a is None or b is None:
        return False
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return a == b


def image_motion_span_phase_for_jobs(
    jobs: list[TimelineComposeJob],
) -> list[tuple[float | None, float | None]]:
    """같은 PNG·같은 줌 효과가 이어지는 구간 — 클립마다 효과가 처음부터 반복되지 않게."""
    from mp4_search.image_effects import PNG_EFFECT_FIXED, normalize_png_effect

    n = len(jobs)
    out: list[tuple[float | None, float | None]] = [(None, None)] * n
    i = 0
    while i < n:
        job = jobs[i]
        img = job.image
        eff = normalize_png_effect(getattr(job, "image_effect", "fixed"))
        if img is None or not img.is_file() or eff == PNG_EFFECT_FIXED or job.is_gap:
            out[i] = (None, None)
            i += 1
            continue
        j = i
        run_start = float(job.mark_sec)
        while j + 1 < n:
            nxt = jobs[j + 1]
            nxt_eff = normalize_png_effect(getattr(nxt, "image_effect", "fixed"))
            if (
                not nxt.is_gap
                and nxt.image is not None
                and _same_compose_image_path(nxt.image, img)
                and nxt_eff == eff
            ):
                j += 1
            else:
                break
        span = sum(float(jobs[k].duration_sec) for k in range(i, j + 1))
        span = max(span, 1e-6)
        for k in range(i, j + 1):
            phase = max(0.0, float(jobs[k].mark_sec) - run_start)
            out[k] = (span, phase)
        i = j + 1
    return out


def format_jobs_timeline_summary(
    jobs: list[TimelineComposeJob],
    *,
    max_lines: int = 8,
) -> str:
    """합성 구간 요약 — ``all.mp4`` 재생 시각 안내."""
    lines: list[str] = []
    pos = 0.0
    for j in jobs:
        end = pos + j.duration_sec
        if j.is_gap:
            label = "(빈 구간)"
        elif j.is_image_only and j.image:
            label = j.image.name
        elif j.video:
            label = j.video.name
            if j.is_hold:
                label += " (정지)"
            elif getattr(j, "video_loop", True):
                label += " (반복)"
            else:
                label += " (유지)"
            if j.image:
                label += f" + {j.image.name}"
        else:
            label = "?"
        lines.append(f"  {pos:g}~{end:g}초 — {label}")
        pos = end
    if len(lines) > max_lines:
        extra = len(lines) - max_lines
        lines = lines[:max_lines]
        lines.append(f"  … 외 {extra}구간")
    return "\n".join(lines)


def format_timeline_compose_status(
    folder: Path,
    segments: list[tuple[float, float]] | None = None,
    *,
    cue_end_times: list[float] | None = None,
    audio_sec: float | None = None,
    extra_mp4: dict[int, Path] | None = None,
    extra_png: dict[int, Path] | None = None,
    asset_start_times: dict[int, float] | None = None,
) -> str:
    mp4_map, png_map = merge_asset_maps(
        *scan_srt_assets(folder),
        extra_mp4=extra_mp4,
        extra_png=extra_png,
    )
    asset_start_times = apply_title_card_asset_times(png_map, asset_start_times)
    lines = [f"폴더: {folder}", "", "SRT_NNN = 파일명 · 시작 = SRT 타임스탬프(초)."]
    scene_gap = infer_scene_interval_from_png_map(png_map)
    if TITLE_CARD_ASSET_KEY in png_map and OPENING_BRIDGE_ASSET_KEY in png_map:
        lines.append(
            f"SRT_000 제목 카드 0~{TITLE_CARD_DURATION_SEC:g}초 · "
            f"첫 본문 SRT_{OPENING_BRIDGE_ASSET_KEY:03d} "
            f"는 {OPENING_BRIDGE_ASSET_KEY:g}초부터."
        )
    elif TITLE_CARD_ASSET_KEY in png_map and scene_gap in png_map:
        lines.append(
            f"SRT_000 제목 카드 0~{TITLE_CARD_DURATION_SEC:g}초 · "
            f"첫 본문 이미지 SRT_{scene_gap:03d} "
            f"는 {TITLE_CARD_DURATION_SEC:g}초부터."
        )
    if mp4_map:
        lines.append("이미지는 해당 MP4 구간에서만 표시 (다음 MP4 시작 시 제거).")
    elif png_map:
        lines.append("MP4 없음 — 이미지 슬라이드쇼로 합성합니다.")
    if not mp4_map and not png_map:
        lines.append("\nSRT_NNN.mp4 / SRT_NNN.png·jpg·gif 파일이 없습니다.")
        return "\n".join(lines)
    missing = missing_timeline_mp4_slots(mp4_map, asset_start_times)
    if missing:
        names = ", ".join(f"SRT_{k:03d}.mp4" for k in missing[:6])
        extra = f" … 외 {len(missing) - 6}개" if len(missing) > 6 else ""
        lines.append(
            f"\n[주의] SRT 시작 슬롯인데 MP4 없음 — 이전 영상이 다음 파일까지 유지됩니다: {names}{extra}"
        )
    if mp4_map:
        lines.append(f"\n[MP4] {len(mp4_map)}개")
        for k in sorted(mp4_map)[:10]:
            st = asset_timeline_mark(k, asset_start_times)
            nxt = next_timeline_mark_after(
                mp4_map,
                png_map,
                st,
                asset_start_times=asset_start_times,
            )
            end_l = f"{nxt:g}초~" if nxt else "끝~"
            lines.append(f"  · {mp4_map[k].name}  (시작 {st:g}초 → {end_l})")
        if len(mp4_map) > 10:
            lines.append(f"  … 외 {len(mp4_map) - 10}개")
    if png_map:
        lines.append(f"\n[이미지] {len(png_map)}개")
        for k in sorted(png_map)[:10]:
            st = asset_timeline_mark(k, asset_start_times)
            lines.append(f"  · {png_map[k].name}  (시작 {st:g}초~)")
        if len(png_map) > 10:
            lines.append(f"  … 외 {len(png_map) - 10}개")
    if segments:
        jobs = list_timeline_compose_jobs(
            folder,
            segments,
            cue_end_times=cue_end_times,
            audio_sec=audio_sec,
            extra_mp4=extra_mp4,
            extra_png=extra_png,
            asset_start_times=asset_start_times,
        )
        total = timeline_end_sec(
            segments,
            mp4_map,
            png_map,
            cue_end_times=cue_end_times,
            audio_sec=audio_sec,
            asset_start_times=asset_start_times,
        )
        lines.append(f"\n[합성 구간] {len(jobs)}클립 · 타임라인 ~{total:g}초 → {ALL_MP4_NAME}")
        for job in jobs[:8]:
            at = f"{job.mark_sec:g}"
            if job.is_gap:
                lines.append(f"  · {job.duration_sec:g}초 @ {at}초 ← (빈 구간)")
                continue
            if job.is_image_only:
                img = job.image.name if job.image else "?"
                lines.append(f"  · {job.duration_sec:g}초 @ {at}초 ← {img} (이미지)")
                continue
            if job.is_hold:
                vid = job.video.name if job.video else "?"
                lines.append(f"  · {job.duration_sec:g}초 @ {at}초 ← {vid} (정지)")
                continue
            img = job.image.name if job.image else "(없음)"
            vid = job.video.name if job.video else "?"
            lines.append(
                f"  · {job.duration_sec:g}초 @ {at}초 ← {vid}"
                + (f" + {img}" if job.image else "")
            )
        if len(jobs) > 8:
            lines.append(f"  … 외 {len(jobs) - 8}클립")
    return "\n".join(lines)


def compose_asset_statuses(
    jobs: list[TimelineComposeJob],
    mp4_map: dict[int, Path],
    asset_start_times: dict[int, float] | None,
    png_map: dict[int, Path] | None = None,
    *,
    expected_slots: set[int] | frozenset[int] | None = None,
) -> dict[int, str]:
    """자산 번호별 합성 결과 — GUI 「합성」 컬럼용."""
    png_map = dict(png_map or {})
    switch: set[int] = set()
    hold: set[int] = set()
    img_used: set[int] = set()
    for j in jobs:
        if j.is_gap:
            continue
        if j.is_image_only:
            if j.image_from is not None:
                img_used.add(int(j.image_from))
            continue
        if not j.video:
            continue
        vf = int(j.video_from)
        if j.is_hold:
            hold.add(vf)
        else:
            switch.add(vf)
        if j.image_from is not None:
            img_used.add(int(j.image_from))

    statuses: dict[int, str] = {}
    keys: set[int] = set(mp4_map.keys()) | set(png_map.keys())
    if expected_slots:
        keys |= {int(x) for x in expected_slots}
    for k in sorted(keys):
        if k in mp4_map:
            if k in switch:
                statuses[k] = "합성완료·연장" if k in hold else "합성완료"
            elif k in hold:
                statuses[k] = "연장만"
            else:
                statuses[k] = "미포함"
        elif k in png_map:
            statuses[k] = "합성완료" if k in img_used else "미포함"
        elif expected_slots and k in expected_slots:
            statuses[k] = "누락"
    return statuses


def format_compose_debug_log(
    *,
    mp4_dir: Path,
    srt_path: Path | None = None,
    mp3_path: Path | None = None,
    mp4_map: dict[int, Path] | None = None,
    png_map: dict[int, Path] | None = None,
    folder_mp4: dict[int, Path] | None = None,
    folder_png: dict[int, Path] | None = None,
    extra_mp4: dict[int, Path] | None = None,
    extra_png: dict[int, Path] | None = None,
    asset_start_times: dict[int, float] | None = None,
    expected_slots: set[int] | frozenset[int] | None = None,
    jobs: list[TimelineComposeJob] | None = None,
    row_lines: list[str] | None = None,
    phase: str = "plan",
    result_path: Path | None = None,
    stopped: bool = False,
) -> str:
    """합성 디버그 로그 — GUI 로그 영역·사용자 제출용."""
    from datetime import datetime

    mp4_map = dict(mp4_map or {})
    png_map = dict(png_map or {})
    lines: list[str] = [
        f"=== 7_3 mp4Search 합성 로그 [{phase}] {datetime.now():%Y-%m-%d %H:%M:%S} ===",
        f"MP4 폴더: {mp4_dir}",
    ]
    if srt_path:
        lines.append(f"SRT: {srt_path}")
    if mp3_path:
        lines.append(f"MP3: {mp3_path}")
    if result_path:
        lines.append(f"결과: {result_path}" + (" (중지)" if stopped else ""))

    if folder_mp4 is not None:
        lines.append("")
        lines.append("[폴더 스캔 MP4]")
        for k in sorted(folder_mp4):
            lines.append(f"  {k:03d} → {folder_mp4[k].name}")
        if not folder_mp4:
            lines.append("  (없음)")

    if folder_png is not None:
        lines.append("")
        lines.append("[폴더 스캔 PNG]")
        for k in sorted(folder_png):
            lines.append(f"  {k:03d} → {folder_png[k].name}")
        if not folder_png:
            lines.append("  (없음)")

    if extra_mp4:
        lines.append("")
        lines.append("[GUI 행 MP4 (병합 전 extra)]")
        for k, p in sorted(extra_mp4.items(), key=lambda x: x[0]):
            num = parse_srt_asset_number(Path(p).name)
            lines.append(f"  키={k} 파일번호={num} → {Path(p).name}")

    if extra_png:
        lines.append("")
        lines.append("[GUI 행 PNG (병합 전 extra)]")
        for k, p in sorted(extra_png.items(), key=lambda x: x[0]):
            num = parse_srt_asset_number(Path(p).name)
            lines.append(f"  키={k} 파일번호={num} → {Path(p).name}")

    lines.append("")
    lines.append("[병합 MP4 맵 — 키=파일명 SRT_NNN 번호]")
    for k in sorted(mp4_map):
        st = asset_timeline_mark(k, asset_start_times)
        lines.append(f"  {k:03d} @ {st:g}초 → {mp4_map[k].name}")

    if png_map:
        lines.append("")
        lines.append("[병합 PNG 맵]")
        for k in sorted(png_map):
            st = asset_timeline_mark(k, asset_start_times)
            lines.append(f"  {k:03d} @ {st:g}초 → {png_map[k].name}")

    if asset_start_times:
        lines.append("")
        lines.append("[SRT 시작 시각 → 파일 번호]")
        for k in sorted(asset_start_times):
            mark = asset_timeline_mark(k, asset_start_times)
            fn = mp4_map.get(k)
            img = png_map.get(k)
            if fn and img:
                fn_s = f"{fn.name} + {img.name}"
            elif fn:
                fn_s = fn.name
            elif img:
                fn_s = img.name
            else:
                fn_s = "(파일 없음)"
            lines.append(f"  SRT_{k:03d} → {mark:g}초 · {fn_s}")

    schedule = asset_mark_schedule(mp4_map, png_map, asset_start_times)
    if schedule:
        lines.append("")
        lines.append("[타임라인 마크 (시작초 → 파일번호)]")
        for t in sorted(schedule):
            lines.append(f"  {t:g}초 → SRT_{schedule[t]:03d}")
    thumb_keys = sorted(
        k for k in set(mp4_map) | set(png_map) if is_thumbnail_asset(k)
    )
    if thumb_keys:
        lines.append("")
        lines.append(
            "[썸네일 — 합성 제외] "
            + ", ".join(f"SRT_{k:03d}" for k in thumb_keys)
        )

    missing = missing_timeline_mp4_slots(mp4_map, asset_start_times)
    if missing:
        lines.append("")
        lines.append("[누락 슬롯 — 이전 MP4가 다음까지 연장]")
        for k in missing:
            st = asset_timeline_mark(k, asset_start_times)
            lines.append(f"  SRT_{k:03d} @ {st:g}초")

    if row_lines:
        lines.append("")
        lines.append("[목록 행 (SRT# · 시작초 · MP4)]")
        lines.extend(f"  {ln}" for ln in row_lines[:40])
        if len(row_lines) > 40:
            lines.append(f"  … 외 {len(row_lines) - 40}행")

    if jobs is not None:
        lines.append("")
        lines.append(f"[합성 클립 {len(jobs)}개]")
        pos = 0.0
        for i, j in enumerate(jobs, 1):
            end = pos + j.duration_sec
            if j.is_gap:
                kind = "빈구간"
            elif getattr(j, "is_image_only", False):
                kind = "이미지"
            elif j.is_hold:
                kind = "연장"
            else:
                kind = "재생"
            vid = j.video.name if j.video else "-"
            img = f" + {j.image.name}" if j.image else ""
            lines.append(
                f"  #{i:02d} {pos:g}~{end:g}초 mark={j.mark_sec:g} "
                f"from={j.video_from:03d} [{kind}] {vid}{img}"
            )
            pos = end

        statuses = compose_asset_statuses(
            jobs, mp4_map, asset_start_times, png_map, expected_slots=expected_slots
        )
        if statuses:
            lines.append("")
            lines.append("[자산별 합성 상태]")
            for k in sorted(statuses):
                st = asset_timeline_mark(k, asset_start_times)
                lines.append(f"  SRT_{k:03d} @ {st:g}초 → {statuses[k]}")

        lines.append("")
        lines.append("[재생 시각 요약]")
        lines.append(format_jobs_timeline_summary(jobs, max_lines=20))

    return "\n".join(lines)

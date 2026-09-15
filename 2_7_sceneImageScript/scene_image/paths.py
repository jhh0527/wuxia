# -*- coding: utf-8 -*-
"""기본 경로 · 루트/tts·stt·md·png."""

from __future__ import annotations

from pathlib import Path

from wisdom_workspace import get_workspace_dir, resolve_module_output

MODULE = "2_7_sceneImageScript"
GENSPARK_AI_IMAGE_URL = "https://www.genspark.ai/ai_image"


def module_root() -> Path:
    return Path(__file__).resolve().parents[1]


def module_md_dir() -> Path:
    """이미지 지침·characters.json 은 ``2_5_sceneImage/md`` 를 공유한다.

    코드는 2_5와 분리했지만 md 는 한 벌만 두고 같이 쓴다 (사본이 낡는 것 방지).
    자체 md 를 쓰려면 ``2_7_sceneImageScript/md`` 에 파일을 넣으면 우선한다.
    """
    own = module_root() / "md"
    if any(own.glob("*.txt")) or any(own.glob("*.md")):
        return own
    shared = module_root().parent / "2_5_sceneImage" / "md"
    if shared.is_dir():
        return shared
    return own


def default_root_dir() -> Path:
    ws = get_workspace_dir()
    if ws is not None:
        return ws
    return resolve_module_output(MODULE)


def ensure_root_layout(root: Path | str) -> dict[str, Path]:
    """루트 하위에 tts / stt / md / png / mp3 폴더 생성."""
    r = Path(root).expanduser()
    r.mkdir(parents=True, exist_ok=True)
    out: dict[str, Path] = {}
    for name in ("tts", "stt", "md", "png", "mp3"):
        p = r / name
        p.mkdir(parents=True, exist_ok=True)
        out[name] = p
    return out


def tts_dir(root: Path | str) -> Path:
    return Path(root).expanduser() / "tts"


def stt_dir(root: Path | str) -> Path:
    return Path(root).expanduser() / "stt"


def md_dir_under_root(root: Path | str) -> Path:
    return Path(root).expanduser() / "md"


def png_dir_under_root(root: Path | str) -> Path:
    return Path(root).expanduser() / "png"


def default_png_dir() -> Path:
    return png_dir_under_root(default_root_dir())


def default_script_file() -> Path | None:
    return None


def _newest_file(folder: Path, patterns: tuple[str, ...]) -> Path | None:
    if not folder.is_dir():
        return None
    found: list[Path] = []
    try:
        for pat in patterns:
            found.extend(folder.glob(pat))
    except OSError:
        return None
    files = [p for p in found if p.is_file()]
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


def find_srt_in_stt(root: Path | str) -> Path | None:
    """디폴트 SRT: ``new.srt`` → ``all.srt`` (mp3 우선, 없으면 stt)."""
    return find_default_srt(root)


def find_default_srt(root: Path | str) -> Path | None:
    """``{root}/mp3|stt`` 에서 new.srt → all.srt, 없으면 최신 *.srt."""
    r = Path(root).expanduser()
    for folder in (r / "mp3", r / "stt"):
        for name in ("new.srt", "all.srt"):
            p = folder / name
            try:
                if p.is_file():
                    return p.resolve()
            except OSError:
                continue
    for folder in (r / "mp3", r / "stt"):
        found = _newest_file(folder, ("*.srt", "*.SRT"))
        if found is not None:
            return found
    return None


def find_prompt_in_md(root: Path | str) -> Path | None:
    """루트/md 이미지 프롬프트 (image* 우선, 아니면 최신 txt/md)."""
    md = md_dir_under_root(root)
    if not md.is_dir():
        return None
    image_first = _newest_file(md, ("image*.txt", "image*.md", "Image*.txt"))
    if image_first is not None:
        return image_first
    # SRT_XXX 씬이 들어 있는 파일 우선
    try:
        candidates = sorted(
            [p for p in md.iterdir() if p.is_file() and p.suffix.lower() in {".txt", ".md"}],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return None
    for p in candidates:
        if parseable_scene_file(p):
            return p.resolve()
    return candidates[0].resolve() if candidates else None


def find_all_srt_for_png(png_dir: str | Path) -> Path | None:
    """호환: png 부모를 루트로 보고 stt SRT, 없으면 예전 mp3/all.srt."""
    png = Path(png_dir).expanduser()
    try:
        png = png.resolve()
    except OSError:
        pass
    root = png.parent if png.name.casefold() == "png" else png
    found = find_srt_in_stt(root)
    if found is not None:
        return found
    # 레거시
    for c in (
        root / "mp3" / "new.srt",
        root / "mp3" / "all.srt",
        root / "stt" / "new.srt",
        root / "stt" / "all.srt",
        root / "all.srt",
    ):
        try:
            if c.is_file():
                return c.resolve()
        except OSError:
            continue
    return None


def _find_prompt_in_module_md() -> Path | None:
    """모듈 ``2_5_sceneImage/md`` — image* 우선, 없으면 최신 txt/md."""
    md = module_md_dir()
    md.mkdir(parents=True, exist_ok=True)
    image_first = _newest_file(
        md, ("image*.txt", "image*.md", "Image*.txt", "Image*.md")
    )
    if image_first is not None:
        return image_first
    found_list: list[Path] = []
    try:
        found_list.extend(
            sorted(
                [
                    p
                    for p in md.iterdir()
                    if p.is_file() and p.suffix.lower() in {".txt", ".md"}
                ],
                key=lambda x: x.stat().st_mtime,
                reverse=True,
            )
        )
    except OSError:
        return None
    return found_list[0].resolve() if found_list else None


def find_image_prompt_file(
    *,
    preferred: str | Path | None = None,
    root: Path | str | None = None,
) -> Path | None:
    """디폴트: 모듈 ``md/`` (변경 가능). 없으면 루트/md."""
    if preferred:
        p = Path(preferred).expanduser()
        try:
            if p.is_file():
                return p.resolve()
        except OSError:
            pass
    found = _find_prompt_in_module_md()
    if found is not None:
        return found
    if root:
        return find_prompt_in_md(root)
    return None


def read_text_file(path: str | Path | None) -> str:
    if not path:
        return ""
    p = Path(path)
    if not p.is_file():
        return ""
    try:
        return p.read_text(encoding="utf-8-sig")
    except OSError:
        return ""


def build_interval_command(interval_sec: int) -> str:
    """브라우저에 넣을 생성 간격 안내 명령."""
    n = int(interval_sec) if interval_sec else 20
    return f"생성 간격 {n}초로 이미지를 생성해줘"


def build_paste_payload(
    prompt_path: str | Path | None,
    srt_path: str | Path | None,
    *,
    interval_sec: int | None = None,
) -> str:
    """브라우저 입력창에 넣을 이미지프롬프트 + SRT 대본.

    둘 다 있을 때만 완전한 페이로드. 구간 헤더로 구분한다.
    """
    prompt = read_text_file(prompt_path).strip()
    srt = read_text_file(srt_path).strip()
    parts: list[str] = []
    if prompt:
        parts.append("===== IMAGE PROMPT =====\n" + prompt)
    if srt:
        parts.append("===== SRT =====\n" + srt)
    if interval_sec is not None:
        parts.append(build_interval_command(interval_sec))
    return "\n\n".join(parts).strip()


def paste_payload_stats(
    prompt_path: str | Path | None,
    srt_path: str | Path | None,
) -> dict[str, int | str | bool]:
    """붙여넣기 전 프롬프트/SRT 유효성·길이."""
    pp = Path(prompt_path).expanduser() if prompt_path else None
    sp = Path(srt_path).expanduser() if srt_path else None
    prompt = read_text_file(pp).strip() if pp else ""
    srt = read_text_file(sp).strip() if sp else ""
    return {
        "prompt_path": str(pp) if pp else "",
        "srt_path": str(sp) if sp else "",
        "prompt_ok": bool(pp and pp.is_file() and prompt),
        "srt_ok": bool(sp and sp.is_file() and srt),
        "prompt_chars": len(prompt),
        "srt_chars": len(srt),
        "has_srt_timecode": ("-->" in srt),
    }


def find_sst_for_png(png_dir: str | Path) -> Path | None:
    png = Path(png_dir).expanduser()
    try:
        png = png.resolve()
    except OSError:
        pass
    root = png.parent if png.name.casefold() == "png" else png
    return find_prompt_in_md(root)


def parseable_scene_file(path: Path) -> bool:
    try:
        from .scene_parse import parse_scene_script

        return bool(parse_scene_script(path.read_text(encoding="utf-8-sig")))
    except Exception:
        return False


def load_scene_text(
    *,
    prompt_path: str | Path | None = None,
    png_dir: str | Path | None = None,
    fallback_text: str = "",
) -> str:
    """SRT_XXX 씬 본문 — 프롬프트 파일 → md → 설정 캐시."""
    from .scene_parse import parse_scene_script

    if prompt_path:
        t = read_text_file(prompt_path)
        if t.strip() and parse_scene_script(t):
            return t
    if png_dir:
        sst = find_sst_for_png(png_dir)
        if sst is not None:
            return read_text_file(sst)
    return fallback_text or ""

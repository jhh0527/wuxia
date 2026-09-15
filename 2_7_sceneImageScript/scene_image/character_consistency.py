# -*- coding: utf-8 -*-
"""씬별 Face Identity·LOOK 자동 삽입 · (선택) State Layer 추적.

인물 LOOK·탐지·상태는 ``characters.json``(CharacterRegistry)에서 로드한다.
``STATE_TRACKING_ENABLED=False`` 이면 대본 상태 전환·``.character_state.json`` 미사용,
항상 base LOOK(+ no blood)만 붙인다.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from scene_image.character_bible import CharacterDef, CharacterRegistry, get_registry

# False: 대본으로 blood_robe 등 상태를 바꾸지 않음 · .character_state.json 미사용
# Face LOOK은 항상 default_states(base) 기준으로만 삽입
STATE_TRACKING_ENABLED = False

# 아래는 STATE_TRACKING_ENABLED=True 일 때만 사용
_BATTLE_CTX_RE = re.compile(
    r"전투|격투|싸움|대결|결투|혈전|비무|대치|"
    r"베어|베였|찔러|찔렸|칼로|검에|검기|칼끝|일격|검광|"
    r"duel|battle|clash|fight|combat|slash|stab",
    re.I,
)
_BLOOD_STATES = frozenset({"blood_robe", "wounded_shoulder"})
_COMBAT_PRIOR = frozenset({"battle_torn", "blood_robe", "wounded_shoulder"})
# 추적 OFF일 때: 이 씬 대본에 부상·출혈이 보이면 no-blood 강제 문구를 붙이지 않음
_WOUND_SCENE_RE = re.compile(
    r"상처|부상|다치|출혈|선혈|핏물|피\s*묻|피\s*흘|피\s*튀|베인|찔린|"
    r"찢긴|찢어|피범벅|blood|wound|bleed|injured|slash|stab",
    re.I,
)

_STYLE_COMMON = (
    "Chinese wuxia manhua (중국 무협 만화) style illustration, bold ink outlines "
    "with dramatic ink-wash and vibrant cel-shaded coloring, flowing ancient "
    "Chinese hanfu robes, ornate jian sword ornaments and swirling qi energy "
    "effects, cinematic high-contrast lighting with mystical cyan/crimson/golden "
    "aura, classic Chinese manhua panel composition, epic mountain-river jianghu "
    "backdrop, 16:9 aspect ratio, ultra high resolution digital manhua art, "
    "no Studio Ghibli style, no Korean manhwa, no Japanese anime, no 3D render, "
    "no flat cartoon, no photoreal, no live-action photo, "
    "no speech bubbles, no comic balloons, no dialogue balloons, no thought bubbles, "
    "no tooltips, no help balloons, no UI tips, no callout boxes, no caption boxes, "
    "no chat bubbles, no watermarks, no logos, "
    "no Hangul, no Korean characters, no Korean written text on image, "
    "no hanbok, no Korean traditional clothing, no jeogori, no chima, "
    "no Japanese text on image, "
    "no English text on image, no Latin letters on image, no Chinese hanzi on image, "
    "no written text, no letters, no captions, no signs with readable writing"
)

_STYLE_TAIL_CHARACTER = (
    _STYLE_COMMON
    + ", distinct individualized faces matching Character Bible Face Identity, "
    "each character visually unique, NOT generic bishonen, NOT same face as other "
    "disciples, expression follows each character's Face Identity (not uniform "
    "heroic pretty-boy look)"
)

_STYLE_TAIL_LANDSCAPE = (
    _STYLE_COMMON
    + ", dynamic martial arts action pose, scenic atmospheric composition"
)

# 하위 호환 — 인물 없는 장면 기본
_STYLE_TAIL = _STYLE_TAIL_LANDSCAPE


def style_tail_for_scene(*, has_characters: bool) -> str:
    """인물 장면 / 풍경-only 장면용 §6 스타일 꼬리."""
    return _STYLE_TAIL_CHARACTER if has_characters else _STYLE_TAIL_LANDSCAPE


def build_contrast_clause(
    present: list[str],
    registry: CharacterRegistry | None = None,
) -> str:
    """2인 이상 등장 시 얼굴 대비 문장."""
    reg = registry or CharacterRegistry()
    return reg.build_contrast_clause(present)


def _fill_look(template: str, *, face: str, state: str, expression: str) -> str:
    return (
        template.replace("{face}", face)
        .replace("{state}", state)
        .replace("{expression}", expression or "")
    )


def _character_look(
    ch: CharacterDef,
    dialogue: str,
    state: str,
) -> tuple[str, int | None, bool]:
    """LOOK 문자열, 나이(표시용), skip_anchor 여부."""
    for var in ch.look_variants:
        if var.pattern.search(dialogue or ""):
            return var.look, None, var.skip_anchor

    if not ch.has_states:
        return (ch.look or "").strip(), None, False

    body = ch.states.get(state) or ch.states.get("base") or ""
    age = ch.default_age
    for pat, a in ch.age_rules:
        if pat.search(dialogue or ""):
            age = a
            break
    expr = ch.default_expression
    for rule in ch.emotion_rules:
        if rule.pattern.search(dialogue or ""):
            expr = rule.value
            break

    tmpl = ch.look_by_age.get(str(age)) or ch.look_template or ch.look
    if not tmpl:
        return "", age, False
    look = _fill_look(tmpl, face=ch.face, state=body, expression=expr)
    return look, age, False


@dataclass
class CharacterStateTracker:
    """인물 LOOK 조립. 상태 추적이 켜진 경우에만 씬 간 state·json 유지."""

    states: dict[str, str] = field(default_factory=dict)
    registry: CharacterRegistry | None = None
    default_protagonist: str = ""

    def __post_init__(self) -> None:
        reg = self.registry
        if reg is not None:
            if not self.default_protagonist:
                self.default_protagonist = reg.protagonist_id
            if not self.states and reg.default_states:
                self.states = dict(reg.default_states)
            elif not STATE_TRACKING_ENABLED and reg.default_states:
                # 추적 OFF: 항상 default(base)로 고정
                self.states = dict(reg.default_states)

    @classmethod
    def load(
        cls,
        png_dir: Path | str | None = None,
        *,
        registry: CharacterRegistry | None = None,
    ) -> CharacterStateTracker:
        reg = registry or CharacterRegistry()
        if not STATE_TRACKING_ENABLED:
            return cls(registry=reg)
        path = _state_path(png_dir)
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get("states"), dict):
                    st = {str(k): str(v) for k, v in data["states"].items()}
                    return cls(states=st, registry=reg)
            except (OSError, json.JSONDecodeError, TypeError):
                pass
        return cls(registry=reg)

    def save(self, png_dir: Path | str | None) -> None:
        if not STATE_TRACKING_ENABLED:
            return
        path = _state_path(png_dir)
        if path.parent:
            path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.write_text(
                json.dumps({"states": self.states}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass

    def _reg(self) -> CharacterRegistry:
        return self.registry or CharacterRegistry()

    def summary(self) -> str:
        if not STATE_TRACKING_ENABLED:
            return "tracking=OFF; " + "; ".join(
                f"{k}|{v}" for k, v in sorted(self.states.items())
            )
        parts = [f"{k}|{v}" for k, v in sorted(self.states.items())]
        return "; ".join(parts)

    def detect_present(
        self,
        dialogue: str,
        scene_prompt: str | None = None,
    ) -> list[str]:
        reg = self._reg()
        if reg.characters:
            found = reg.detect_present(dialogue, scene_prompt)
            if found:
                return found
        if self.default_protagonist:
            return [self.default_protagonist]
        return []

    def infer_state(self, cid: str, dialogue: str) -> str | None:
        if not STATE_TRACKING_ENABLED:
            return None
        ch = self._reg().get(cid)
        if ch is None or not ch.state_rules:
            return None
        t = dialogue or ""
        cur = self.states.get(cid, "base")
        for rule in ch.state_rules:
            if not rule.pattern.search(t):
                continue
            if rule.value in _BLOOD_STATES:
                if cur not in _COMBAT_PRIOR and not _BATTLE_CTX_RE.search(t):
                    continue
            return rule.value
        return None

    def update_from_dialogue(
        self,
        dialogue: str,
        *,
        scene_prompt: str | None = None,
    ) -> None:
        if not STATE_TRACKING_ENABLED:
            return
        present = self.detect_present(dialogue, scene_prompt)
        stateful = self._reg().stateful_ids()
        for cid in present:
            if cid not in stateful:
                continue
            new = self.infer_state(cid, dialogue)
            if new:
                self.states[cid] = new

    def build_look_block(
        self,
        dialogue: str,
        scene_prompt: str | None = None,
    ) -> str:
        """매 장 명령에 붙일 CHARACTER LOOK 영문 블록."""
        reg = self._reg()
        if not STATE_TRACKING_ENABLED and reg.default_states:
            self.states = dict(reg.default_states)
        present = self.detect_present(dialogue, scene_prompt)
        lines: list[str] = []
        keys: list[str] = []
        anchors: list[str] = []
        for cid in present:
            ch = reg.get(cid)
            if ch is None:
                continue
            state = self.states.get(cid, "base")
            if not STATE_TRACKING_ENABLED:
                state = "base"
            look, age, skip_anchor = _character_look(ch, dialogue, state)
            if not look:
                continue
            # 추적 OFF: 평상시는 no-blood, 부상·출혈 대본이면 모델이 피를 그릴 수 있게 둠
            wound_scene = bool(_WOUND_SCENE_RE.search(dialogue or ""))
            if (
                ch.has_states
                and not skip_anchor
                and not wound_scene
                and (not STATE_TRACKING_ENABLED or state not in _BLOOD_STATES)
            ):
                look = (
                    look.rstrip(" .,")
                    + ", no blood on robes or skin, no bleeding wounds"
                )
            lines.append(look)
            if ch.has_states and age is not None:
                keys.append(f"{cid}≈{age}|{state}")
            elif ch.has_states:
                keys.append(f"{cid}|{state}")
            else:
                keys.append(f"{cid}|base")
            if ch.anchor and not skip_anchor:
                anchors.append(ch.anchor)
        if not lines:
            return ""
        header = f"State keys: {'; '.join(keys)}"
        if not STATE_TRACKING_ENABLED:
            header = f"Look keys (state tracking OFF): {'; '.join(keys)}"
        block = header + "\n" + "\n".join(lines)
        contrast = reg.build_contrast_clause(present)
        if contrast:
            block += "\n" + contrast
        for a in anchors:
            block += "\n" + a
        return block.strip()

    def build_scene_instruction(self, label: str, *, multi_character: bool = False) -> str:
        """생성 지시 — Face 유지, 장면만 변경."""
        distinct = (
            " When multiple characters appear, keep each face visually distinct — "
            "do NOT merge into one generic bishonen face."
            if multi_character
            else ""
        )
        if STATE_TRACKING_ENABLED:
            consistency = (
                "Keep Face Identity and State keys in CHARACTER LOOK identical to the "
                "previous current-timeline scene unless dialogue changed hair/robe/wound; "
                "only pose, action, camera, and background may change. "
                "Do NOT redesign face, hair style, or robe colors arbitrarily. "
            )
        else:
            consistency = (
                "Keep Face Identity and base outfit in CHARACTER LOOK identical across "
                "scenes; only pose, action, camera, and background may change. "
                "Do NOT redesign face, hair, or robe colors. "
                "Do NOT add blood, wounds, or torn robes unless the dialogue clearly "
                "shows this character injured in combat in this scene. "
            )
        return (
            f"{label} Chinese wuxia manhua illustration — generate this scene now."
            f"{distinct} "
            f"{consistency}"
            f"No text, speech bubbles, comic balloons, dialogue balloons, thought bubbles, "
            f"tooltips, callouts, caption boxes, or any on-image writing. "
            f"Show speech only via expression and pose — never draw a balloon. "
            f"After the image appears, output exactly one line and nothing else: "
            f"「{label} 이미지가 성공적으로 생성되었습니다.」 "
            f"Do not write success text without the image. "
            f"If generation fails, short error only. "
            f"Strictly forbidden after the image: scene description, caption, summary, "
            f"analysis, commentary, tips, next-scene suggestions, verification table, "
            f"checklist, matching notes, background guide, continuation, Q&A, "
            f"or any Korean/English prose other than that single success line."
        )


def _state_path(png_dir: Path | str | None) -> Path:
    if png_dir:
        return Path(png_dir) / ".character_state.json"
    return Path(".character_state.json")


def build_character_look_for_scene(
    srt_sec: int,
    *,
    dialogue: str = "",
    scene_prompt: str | None = None,
    png_dir: Path | str | None = None,
    tracker: CharacterStateTracker | None = None,
    registry: CharacterRegistry | None = None,
    prompt_path: Path | str | None = None,
) -> tuple[str, CharacterStateTracker]:
    """LOOK 블록 반환. 상태 추적이 켜진 경우에만 대본으로 state 갱신·저장."""
    del srt_sec  # 향후 장면별 오버라이드용
    reg = registry or get_registry(prompt_path=prompt_path, png_dir=png_dir)
    if tracker is None:
        tr = CharacterStateTracker.load(png_dir, registry=reg)
    else:
        tr = tracker
        if tr.registry is None:
            tr.registry = reg
            if not tr.default_protagonist:
                tr.default_protagonist = reg.protagonist_id
    if STATE_TRACKING_ENABLED:
        tr.update_from_dialogue(dialogue, scene_prompt=scene_prompt)
    look = tr.build_look_block(dialogue, scene_prompt)
    if STATE_TRACKING_ENABLED and png_dir:
        tr.save(png_dir)
    return look, tr

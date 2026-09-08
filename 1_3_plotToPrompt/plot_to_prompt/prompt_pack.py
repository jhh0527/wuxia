# -*- coding: utf-8 -*-
"""젠스파크에 붙여 넣을 프롬프트 패키지."""

from __future__ import annotations

from pathlib import Path

FALLBACK_WRITE_RULES = """# 검신귀환록 — 젠스파크 작성 규칙

젠스파크 **한 채팅 = 한 장**. 이 규칙 + BRIEF + 직전 장 끝부분만 넣는다.
부 전체 줄거리·앞장 전문은 넣지 않는다.

## 분량
- 목표: 본문 **9,000~11,000자** (제목·주석·메타 제외)
- 부족해도 신규 사건·신규 주요 인물·새 조직·사망·자결·납치로 채우지 말 것
- 밀도만 높일 것: 감각, 내면, 대화 호흡, BRIEF에 적힌 이동·관찰·전투 규모

## 반드시 따를 것
1. BRIEF의 **비트 번호 순서**대로만 진행
2. 등장 인물·호칭·관계는 BRIEF·인물 발췌만 사용
3. 인원·사상 숫자는 BRIEF에 적힌 값 고정
4. 시점 나이·계절·장소를 BRIEF 상단에 맞출 것
5. 다음 장 사건 선취 금지. 종료 훅은 BRIEF에 적힌 한 줄만

## 쓰기 전 승인 (있으면 채팅 중단·사람 확인)
- 새 이름 있는 인물 / 새 문파·당·루
- 대규모 전투, 배신 확정, 사망·자결·납치
- 기존 사건 결과·시간 순서 변경
- events.md에 없는 사건 ID

## 문체
- 한국어 문어체 무협. 한자 병기 가능
- 유튜브 낭독: 짧은 문장과 긴 문장 혼용. 대사·액션은 끊어서
- 상태창·설정 덤프·설명조 나열 금지

## 출력
- 장 제목 한 줄 후 본문만
- 첫 줄 형식: ``제N장-본문제목`` (예: 제14장-절벽의 손님)
- 메타 코멘트·「이어서」·비트 번호 표기 넣지 말 것
- 본문을 <<<CHAPTER_START>>> … <<<CHAPTER_END>>> 로 감싼다 (구분자 밖 금지)
- 끝나면 비트 누락 / 신규 사건 / 글자 수 점검 후 부족·초과만 수정
"""


def load_write_rules(path: Path | None) -> str:
    if path is not None and path.is_file():
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            pass
    return FALLBACK_WRITE_RULES.strip()


def build_genspark_paste(
    *,
    write_rules: str,
    brief_md: str,
    chapter: int,
    cast_excerpt: str = "",
) -> str:
    cast_block = (cast_excerpt or "").strip()
    if not cast_block:
        cast_block = (
            "(여기에 필요 인물만 2~6줄 발췌해 붙이세요. "
            "예: 진무한·한백강 관계·호칭. 부 전체 characters.md는 넣지 말 것.)"
        )

    return f"""# 젠스파크 붙여넣기 — CHAPTER_{chapter:02d}
한 채팅 = 이 장만. 아래 순서대로 따른 뒤, **직전 장 끝 800~1,500자**를 이어서 붙이세요.

━━━━━━━━━━━━━━━━━━━━━━━━
[1] WRITE_RULES
━━━━━━━━━━━━━━━━━━━━━━━━
{write_rules.strip()}

━━━━━━━━━━━━━━━━━━━━━━━━
[2] BRIEF
━━━━━━━━━━━━━━━━━━━━━━━━
{brief_md.strip()}

━━━━━━━━━━━━━━━━━━━━━━━━
[3] 인물 발췌 (짧게)
━━━━━━━━━━━━━━━━━━━━━━━━
{cast_block}

━━━━━━━━━━━━━━━━━━━━━━━━
[4] 직전 장 끝 (사용자가 붙여 넣기)
━━━━━━━━━━━━━━━━━━━━━━━━
(직전 장 본문 마지막 800~1,500자를 여기에 붙여 넣으세요.)

━━━━━━━━━━━━━━━━━━━━━━━━
[지시]
위 BRIEF 비트 순서대로 본문만 작성하세요.
목표 분량 9,000~11,000자. 제목 한 줄 후 본문만 출력.
"""

# -*- coding: utf-8 -*-
"""wuxia 허브 탭 순서 (파이프라인 번호 체계)."""

from __future__ import annotations

# (탭 제목, 모듈 폴더명)
HUB_TABS: tuple[tuple[str, str], ...] = (
    ("1_1 volumePlot", "1_1_volumePlotToPrompt"),
    ("1_3 plotToPrompt", "1_3_plotToPrompt"),
    ("1_5 textToJson", "1_5_textToJson"),
    ("2_2 scriptToVoice", "2_2_scriptToVoice"),
    ("2_3 STT", "2_3_stt"),
    ("2_3_1 mp4ToSrt", "2_3_1_mp4ToSrt"),
    ("2_3_2 VoiceReplace", "2_3_2_mp4VoiceReplace"),
    ("2_4 srtEdit", "2_4_srtEdit"),
    ("2_5 씬이미지", "2_5_sceneImage"),
    ("3_2 PNG→JPG", "3_2_pngToJpg"),
    ("7_3 mp4Search", "7_3_mp4Search"),
    ("7_4 mp4Merge", "7_4_mp4Merge"),
)

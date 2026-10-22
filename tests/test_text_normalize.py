"""繁简归一化测试：Whisper 输出繁体时关键词仍应命中。"""

from __future__ import annotations

from rusty_moment.config.config_manager import KeywordsConfig
from rusty_moment.detection.keyword_detector import KeywordDetector
from rusty_moment.detection.text_normalize import normalize_for_matching, to_simplified


def test_traditional_is_converted():
    assert to_simplified("別管我，你們快走") == "别管我，你们快走"
    assert to_simplified("替我報仇") == "替我报仇"
    assert to_simplified("我盡力了") == "我尽力了"


def test_simplified_passthrough():
    text = "别管我，你们快走"
    assert to_simplified(text) == text


def test_empty_text_is_safe():
    assert to_simplified("") == ""
    assert normalize_for_matching("   ") == ""


def test_punctuation_is_normalized():
    normalized = normalize_for_matching("别管我，你们快走！")
    assert normalized == "别管我,你们快走!"


def test_lowercase_option():
    assert normalize_for_matching("LAST STAND", lower=True) == "last stand"
    assert normalize_for_matching("LAST STAND", lower=False) == "LAST STAND"


def test_keyword_matches_traditional_transcript():
    """真机验证时 Whisper 输出了繁体，这是当时的真实故障场景。"""
    detector = KeywordDetector(
        KeywordsConfig(
            positives=[{"phrase": "别管我", "weight": 1.0}],
            exclusions=[],
        )
    )
    result = detector.analyze("兄弟,別管我,你們快走,我來拖住他們")
    assert result.matched
    assert result.score == 1.0


def test_traditional_keyword_matches_simplified_text():
    detector = KeywordDetector(
        KeywordsConfig(
            positives=[{"phrase": "替我報仇", "weight": 1.0}],
            exclusions=[],
        )
    )
    assert detector.analyze("我尽力了,替我报仇").matched


def test_exclusion_still_works_with_traditional_text():
    detector = KeywordDetector(
        KeywordsConfig(
            positives=[{"phrase": "别管我", "weight": 1.0}],
            exclusions=[{"phrase": "开玩笑", "weight": 1.0}],
        )
    )
    plain = detector.analyze("别管我")
    joked = detector.analyze("別管我,開玩笑的")
    assert joked.matched
    assert joked.score < plain.score

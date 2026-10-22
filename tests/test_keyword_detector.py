"""关键词层测试：正向词、排除词打折、最小命中数、大小写。"""

from __future__ import annotations

from rusty_moment.config.config_manager import KeywordsConfig
from rusty_moment.detection.keyword_detector import KeywordDetector


def make_detector(**kwargs) -> KeywordDetector:
    config = KeywordsConfig(
        positives=[
            {"phrase": "别管我", "weight": 1.0},
            {"phrase": "我尽力了", "weight": 1.0},
            {"phrase": "快走", "weight": 0.5},
        ],
        exclusions=[{"phrase": "开玩笑", "weight": 1.0}],
        **kwargs,
    )
    return KeywordDetector(config)


def test_single_full_weight_hit_reaches_full_score():
    result = make_detector().analyze("别管我了，你们先走")
    assert result.matched
    assert result.score == 1.0
    assert result.hits[0].phrase == "别管我"


def test_half_weight_hit_is_scaled():
    result = make_detector().analyze("你们快走")
    assert result.matched
    assert result.score == 0.5


def test_exclusion_words_discount_not_veto():
    plain = make_detector().analyze("别管我")
    excluded = make_detector().analyze("别管我，开玩笑的")
    assert excluded.matched, "排除词不应硬否决，只是打折"
    assert excluded.score < plain.score
    assert excluded.exclusions


def test_min_hits_requires_multiple_phrases():
    detector = make_detector(min_hits=2)
    single = detector.analyze("别管我")
    assert not single.matched
    both = detector.analyze("别管我，我尽力了")
    assert both.matched


def test_no_hit_returns_zero():
    result = make_detector().analyze("今天天气不错")
    assert result.score == 0.0
    assert not result.matched


def test_disabled_layer_returns_zero():
    detector = make_detector(enabled=False)
    result = detector.analyze("别管我")
    assert result.score == 0.0
    assert not result.matched


def test_empty_text_is_safe():
    result = make_detector().analyze("")
    assert result.score == 0.0
    assert result.describe() == "未命中关键词"


def test_case_sensitivity_for_english():
    config = KeywordsConfig(
        positives=[{"phrase": "last stand", "weight": 1.0}],
        exclusions=[],
        case_sensitive=False,
    )
    detector = KeywordDetector(config)
    assert detector.analyze("this is my LAST STAND").matched

    config.case_sensitive = True
    strict = KeywordDetector(config)
    assert not strict.analyze("this is my LAST STAND").matched
    assert strict.analyze("this is my last stand").matched


def test_score_is_capped_at_one():
    config = KeywordsConfig(
        positives=[
            {"phrase": "别管我", "weight": 1.0},
            {"phrase": "我尽力了", "weight": 1.0},
            {"phrase": "快走", "weight": 1.0},
        ],
        exclusions=[],
    )
    result = KeywordDetector(config).analyze("别管我 我尽力了 快走")
    assert result.score == 1.0

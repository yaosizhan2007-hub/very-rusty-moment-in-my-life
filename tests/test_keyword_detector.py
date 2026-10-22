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


# --------------------------------------------------------------------------- #
# 近义说法（aliases）
# --------------------------------------------------------------------------- #
def make_alias_detector() -> KeywordDetector:
    return KeywordDetector(
        KeywordsConfig(
            positives=[
                {
                    "phrase": "别管我",
                    "weight": 1.0,
                    "aliases": ["别救我", "不要救我", "别理我"],
                },
                {"phrase": "我尽力了", "weight": 1.0, "aliases": ["已经尽力了"]},
            ],
            exclusions=[
                {"phrase": "开玩笑", "weight": 1.0, "aliases": ["逗你玩", "说着玩"]},
            ],
        )
    )


def test_alias_hits_the_main_phrase():
    result = make_alias_detector().analyze("你们别救我了，我自己扛")
    assert result.matched
    assert result.score == 1.0
    assert result.hits[0].phrase == "别管我"
    assert result.hits[0].matched == "别救我"
    assert "≈" in result.hit_text


def test_multiple_aliases_of_same_entry_count_once():
    """同一词条命中多个近义说法时只算一次，避免一句话被重复加分。"""
    single = make_alias_detector().analyze("别救我了")
    multi = make_alias_detector().analyze("别救我，别理我，不要救我")

    assert len(multi.hits) == 1
    assert multi.score == single.score == 1.0


def test_alias_keeps_entry_weight():
    detector = KeywordDetector(
        KeywordsConfig(
            positives=[{"phrase": "我撑不住了", "weight": 0.5, "aliases": ["撑不住了"]}],
            exclusions=[],
        )
    )
    result = detector.analyze("我真的撑不住了")
    assert result.hits[0].phrase == "我撑不住了"
    assert result.score == 0.5


def test_exclusion_alias_discounts_score():
    plain = make_alias_detector().analyze("别管我")
    joked = make_alias_detector().analyze("别管我，逗你玩呢")
    assert joked.matched
    assert joked.score < plain.score
    assert joked.exclusions[0].matched == "逗你玩"


def test_entry_without_alias_still_works():
    detector = KeywordDetector(
        KeywordsConfig(positives=[{"phrase": "别管我", "weight": 1.0}], exclusions=[])
    )
    result = detector.analyze("别管我")
    assert result.matched
    assert result.hits[0].matched == "别管我"
    assert result.hit_text == "别管我"


def test_alias_is_matched_after_traditional_normalization():
    """Whisper 输出繁体 + 近义说法，两个归一化都要生效。"""
    result = make_alias_detector().analyze("你們別救我了")
    assert result.matched
    assert result.hits[0].matched == "别救我"

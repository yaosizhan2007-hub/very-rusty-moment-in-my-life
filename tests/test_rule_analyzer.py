"""规则层测试：声学特征到 0~1 分数的映射。"""

from __future__ import annotations

import pytest

from rusty_moment.config.config_manager import RulesConfig
from rusty_moment.detection.rule_analyzer import RuleAnalyzer
from rusty_moment.detection.vad import UtteranceFeatures


def features(**kwargs) -> UtteranceFeatures:
    base = dict(
        duration_s=3.0,
        voiced_ratio=0.8,
        mean_db=-20.0,
        peak_db=-12.0,
        tail_db=-45.0,
        volume_drop_ratio=0.7,
        longest_pause_s=1.0,
        volume_spike_db=6.0,
    )
    base.update(kwargs)
    return UtteranceFeatures(**base)


def test_long_dramatic_utterance_scores_higher_than_short_shout():
    analyzer = RuleAnalyzer(RulesConfig())
    long_score = analyzer.analyze(features()).score
    short_score = analyzer.analyze(
        features(duration_s=0.4, volume_drop_ratio=0.0, longest_pause_s=0.0, tail_db=-12.0, volume_spike_db=12.0)
    ).score
    assert long_score > short_score


def test_disabled_returns_zero():
    result = RuleAnalyzer(RulesConfig(enabled=False)).analyze(features())
    assert result.score == 0.0
    assert "关闭" in "".join(result.notes)


def test_single_component_weight_dominates():
    config = RulesConfig(
        weights={
            "speech_duration": 1.0,
            "pause_after": 0.0,
            "volume_drop": 0.0,
            "volume_spike": 0.0,
            "silence_after": 0.0,
        },
        speech_duration_full_score_s=4.0,
    )
    result = RuleAnalyzer(config).analyze(features(duration_s=2.0))
    assert result.score == pytest.approx(0.5, abs=1e-6)


def test_all_zero_weights_is_safe():
    config = RulesConfig(
        weights={
            "speech_duration": 0.0,
            "pause_after": 0.0,
            "volume_drop": 0.0,
            "volume_spike": 0.0,
            "silence_after": 0.0,
        }
    )
    result = RuleAnalyzer(config).analyze(features())
    assert result.score == 0.0


def test_score_always_in_range():
    analyzer = RuleAnalyzer(RulesConfig())
    wild = analyzer.analyze(
        features(duration_s=999.0, volume_drop_ratio=5.0, longest_pause_s=99.0, volume_spike_db=99.0)
    )
    assert 0.0 <= wild.score <= 1.0
    assert all(0.0 <= value <= 1.0 for value in wild.components.values())


def test_low_voice_ratio_is_noted():
    analyzer = RuleAnalyzer(RulesConfig())
    result = analyzer.analyze(features(voiced_ratio=0.1))
    assert any("语音占比" in note for note in result.notes)


def test_describe_mentions_dominant_component():
    analyzer = RuleAnalyzer(RulesConfig())
    text = analyzer.analyze(features()).describe()
    assert "规则" in text

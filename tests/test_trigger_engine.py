"""触发引擎测试：归一化、层开关、模式概率、AI 回退、冷却与去重。"""

from __future__ import annotations

import random

import pytest

from rusty_moment.config.config_manager import AppConfig
from rusty_moment.detection.emotion_analyzer import EmotionScores
from rusty_moment.detection.keyword_detector import KeywordHit, KeywordResult
from rusty_moment.detection.rule_analyzer import RuleResult
from rusty_moment.detection.trigger_engine import (
    CandidateEvent,
    DedupeTracker,
    TriggerEngine,
)
from rusty_moment.detection.vad import UtteranceFeatures


class FakeCooldown:
    def __init__(self, active: bool = False, remaining: float = 12.0) -> None:
        self.active = active
        self._remaining = remaining

    def is_active(self) -> bool:
        return self.active

    def remaining(self) -> float:
        return self._remaining


def make_engine(
    config: AppConfig | None = None,
    *,
    seed: int = 7,
    cooldown: FakeCooldown | None = None,
    dedupe: DedupeTracker | None = None,
):
    cfg = config or AppConfig()
    holder = {"cfg": cfg}
    engine = TriggerEngine(
        lambda: holder["cfg"],
        rng=random.Random(seed),
        cooldown=cooldown,
        dedupe=dedupe,
    )
    return engine, holder


def make_candidate(
    text: str = "别管我，你们快走",
    *,
    keyword_score: float | None = 1.0,
    rule_score: float | None = 0.2,
    ai: EmotionScores | None = None,
    duration: float = 2.5,
) -> CandidateEvent:
    features = UtteranceFeatures(
        duration_s=duration,
        voiced_ratio=0.8,
        mean_db=-20.0,
        peak_db=-12.0,
        tail_db=-40.0,
        volume_drop_ratio=0.6,
        longest_pause_s=0.9,
        volume_spike_db=6.0,
    )
    keyword = None
    if keyword_score is not None:
        keyword = KeywordResult(
            score=keyword_score,
            hits=[KeywordHit("别管我", 1.0, 0, 3)],
            matched=keyword_score > 0,
        )
    rule = None
    if rule_score is not None:
        rule = RuleResult(score=rule_score, components={"speech_duration": rule_score})
    return CandidateEvent(
        text=text, features=features, keyword=keyword, rule=rule, ai=ai
    )


def test_weights_are_renormalized_over_active_layers():
    engine, _holder = make_engine()
    decision = engine.evaluate(make_candidate(keyword_score=1.0, rule_score=0.0))
    assert decision.trigger
    # 关键词 1.0、规则 0.0 → 归一化后 keyword 权重应为 2/3
    assert decision.weighted["keyword"] == pytest.approx(2 / 3 * 1.0, abs=1e-6)
    assert decision.combined == pytest.approx(2 / 3, abs=1e-6)


def test_threshold_blocks_low_score():
    engine, holder = make_engine()
    holder["cfg"].trigger.trigger_threshold = 0.9
    decision = engine.evaluate(make_candidate(keyword_score=0.5, rule_score=0.0))
    assert not decision.trigger
    assert decision.rejected_by == "分数不足"
    assert "最低置信度阈值" in decision.reason


def test_disabled_keyword_layer_uses_rule_only():
    engine, holder = make_engine()
    holder["cfg"].keywords.enabled = False
    decision = engine.evaluate(make_candidate(keyword_score=1.0, rule_score=0.9))
    assert "keyword" not in decision.scores
    assert decision.scores == {"rule": 0.9}
    assert decision.trigger


def test_all_layers_disabled_rejects_cleanly():
    engine, holder = make_engine()
    holder["cfg"].keywords.enabled = False
    holder["cfg"].rules.enabled = False
    decision = engine.evaluate(make_candidate())
    assert not decision.trigger
    assert decision.rejected_by == "没有可用的分析层"


def test_serious_mode_probability_zero_never_triggers():
    engine, holder = make_engine()
    holder["cfg"].trigger.probabilities["serious"] = 0.0
    decision = engine.evaluate(make_candidate(keyword_score=1.0))
    assert not decision.trigger
    assert decision.rejected_by == "概率未命中"
    assert decision.sampled is True


def test_serious_mode_low_confidence_branch():
    engine, holder = make_engine()
    holder["cfg"].trigger.trigger_threshold = 0.8
    holder["cfg"].trigger.low_confidence_threshold = 0.3
    holder["cfg"].trigger.probabilities["low_confidence"] = 1.0
    decision = engine.evaluate(make_candidate(keyword_score=0.5, rule_score=0.0))
    assert decision.probability == 1.0
    assert decision.trigger


def test_chaotic_mode_samples_even_low_scores():
    engine, holder = make_engine()
    holder["cfg"].mode.current = "chaotic"
    holder["cfg"].trigger.trigger_threshold = 0.9
    holder["cfg"].trigger.probabilities["chaotic_low_score"] = 1.0
    decision = engine.evaluate(make_candidate(text="今天吃啥", keyword_score=0.0, rule_score=0.05))
    assert decision.trigger, "抽象整活模式下低分也应允许抽样"
    assert decision.mode == "chaotic"


def test_chaotic_high_score_uses_chaotic_probability():
    engine, holder = make_engine()
    holder["cfg"].mode.current = "chaotic"
    holder["cfg"].trigger.probabilities["chaotic"] = 0.0
    decision = engine.evaluate(make_candidate(keyword_score=1.0))
    assert not decision.trigger


def test_ai_unavailable_uses_fallback_probability():
    engine, holder = make_engine()
    holder["cfg"].ai.enabled = True
    holder["cfg"].trigger.probabilities["ai_unavailable"] = 0.0
    decision = engine.evaluate(make_candidate(keyword_score=1.0, ai=None))
    assert not decision.trigger
    assert decision.probability == 0.0
    assert "ai" not in decision.scores, "AI 不可用时不应给 AI 层权重"


def test_ai_layer_participates_when_available():
    engine, holder = make_engine()
    holder["cfg"].ai.enabled = True
    holder["cfg"].trigger.trigger_threshold = 0.4
    ai = EmotionScores(
        dramatic_score=0.9, context_score=0.8, comedic_potential=0.2, reason="台词像遗言", confidence=0.9
    )
    decision = engine.evaluate(make_candidate(keyword_score=0.0, rule_score=0.0, ai=ai))
    assert "ai" in decision.scores
    assert decision.scores["ai"] > 0.7
    # AI 层权重 0.55，但另外两层给了 0 分，合成分被稀释到 0.55 × AI 分
    assert decision.combined == pytest.approx(decision.scores["ai"] * 0.55, abs=1e-6)
    assert decision.trigger


def test_zero_score_layers_dilute_combined_score():
    """设计说明：有结果但 0 分的层是「反面证据」，会拉低合成分（不是被忽略）。"""
    engine, holder = make_engine()
    holder["cfg"].ai.enabled = True
    holder["cfg"].trigger.trigger_threshold = 0.6
    ai = EmotionScores(
        dramatic_score=1.0, context_score=1.0, comedic_potential=0.0, reason="", confidence=1.0
    )
    decision = engine.evaluate(make_candidate(keyword_score=0.0, rule_score=0.0, ai=ai))
    assert decision.combined == pytest.approx(0.55, abs=1e-6)
    # 满分 AI 判断被两层 0 分拉到主阈值以下，只能走「低置信度」概率档
    assert decision.combined < holder["cfg"].trigger.trigger_threshold
    assert decision.tier == "low_confidence"
    assert decision.probability == pytest.approx(0.35)


def test_cooldown_blocks_before_scoring():
    engine, _holder = make_engine(cooldown=FakeCooldown(active=True))
    decision = engine.evaluate(make_candidate())
    assert not decision.trigger
    assert decision.rejected_by == "冷却中"


def test_dedupe_blocks_same_sentence():
    clock = {"now": 100.0}
    dedupe = DedupeTracker(8.0, clock=lambda: clock["now"])
    engine, _holder = make_engine(dedupe=dedupe)

    first = engine.evaluate(make_candidate())
    assert first.trigger
    second = engine.evaluate(make_candidate())
    assert not second.trigger
    assert second.rejected_by == "重复语句"

    clock["now"] += 9.0
    third = engine.evaluate(make_candidate())
    assert third.trigger, "去重窗口过期后应允许再次触发"


def test_short_voice_is_rejected():
    engine, holder = make_engine()
    holder["cfg"].trigger.min_voice_seconds = 1.0
    decision = engine.evaluate(make_candidate(duration=0.4))
    assert not decision.trigger
    assert decision.rejected_by == "语音过短"


def test_same_seed_reproduces_decision():
    results = []
    for _ in range(2):
        engine, holder = make_engine(seed=42)
        holder["cfg"].trigger.probabilities["serious"] = 0.5
        results.append([engine.evaluate(make_candidate()).trigger for _ in range(5)])
    assert results[0] == results[1], "注入 RNG 后结果必须可复现"


def test_zero_weights_falls_back_to_equal_weights():
    engine, holder = make_engine()
    holder["cfg"].trigger.weights = {"ai": 0.0, "keyword": 0.0, "rule": 0.0}
    decision = engine.evaluate(make_candidate(keyword_score=1.0, rule_score=0.0))
    assert decision.combined == pytest.approx(0.5, abs=1e-6), "权重全 0 时应均分而不是除零"


def test_decision_contains_event_id_and_reason():
    engine, _holder = make_engine()
    decision = engine.evaluate(make_candidate())
    assert decision.trigger
    assert len(decision.event_id) == 12
    assert "关键词" in decision.reason

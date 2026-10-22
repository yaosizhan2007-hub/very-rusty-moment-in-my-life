"""触发引擎：把关键词层、规则层、AI 层归一化后合成一个可解释的决策。

核心规则（对应提示词 4.5 / 五）：
1. 禁止把未归一化的原始数值直接相加：权重先按「实际启用的层」重新归一化。
2. 任一层都可以被用户关掉；关掉的层不参与归一化。
3. AI 不可用时自动退回关键词+规则，并使用「AI 不可用」的触发概率。
4. 两种模式只在「概率策略」上不同，评分算法完全一致——这样行为差异可测。
5. 随机数生成器可注入，测试可复现；生产环境用普通 random.Random。
"""

from __future__ import annotations

import logging
import random
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Protocol

from ..config.config_manager import AppConfig
from .emotion_analyzer import EmotionScores
from .keyword_detector import KeywordResult
from .rule_analyzer import RuleResult
from .vad import UtteranceFeatures

log = logging.getLogger(__name__)

MODE_SERIOUS = "serious"
MODE_CHAOTIC = "chaotic"


class CooldownLike(Protocol):
    def is_active(self) -> bool: ...
    def remaining(self) -> float: ...


@dataclass
class CandidateEvent:
    """一次候选事件：文本 + 声学特征 + 三层分析结果。"""

    text: str = ""
    features: UtteranceFeatures | None = None
    keyword: KeywordResult | None = None
    rule: RuleResult | None = None
    ai: EmotionScores | None = None
    ai_error: str = ""
    transcript_source: str = "none"
    at: float = 0.0


@dataclass
class TriggerDecision:
    trigger: bool
    reason: str
    mode: str
    combined: float = 0.0
    scores: dict[str, float] = field(default_factory=dict)
    weighted: dict[str, float] = field(default_factory=dict)
    probability: float = 0.0
    sampled: bool = False
    rejected_by: str = ""
    event_id: str = ""
    tier: str = ""

    def describe(self) -> str:
        return f"{self.reason}（合成分 {self.combined:.2f}，概率 {self.probability:.0%}）"


class DedupeTracker:
    """同一句话在窗口期内不重复触发（语音识别常常重复出同样的结果）。"""

    def __init__(self, window_s: float = 8.0, clock: Callable[[], float] = time.monotonic) -> None:
        self.window_s = window_s
        self._clock = clock
        self._seen: dict[str, float] = {}

    def update_window(self, window_s: float) -> None:
        self.window_s = window_s

    def is_duplicate(self, text: str) -> bool:
        if not text or self.window_s <= 0:
            return False
        now = self._clock()
        self._prune(now)
        last = self._seen.get(text)
        return last is not None and (now - last) <= self.window_s

    def remember(self, text: str) -> None:
        if not text:
            return
        self._seen[text] = self._clock()

    def clear(self) -> None:
        self._seen.clear()

    def _prune(self, now: float) -> None:
        expired = [key for key, stamp in self._seen.items() if now - stamp > self.window_s]
        for key in expired:
            self._seen.pop(key, None)


class TriggerEngine:
    def __init__(
        self,
        config_provider: Callable[[], AppConfig],
        *,
        rng: random.Random | None = None,
        clock: Callable[[], float] = time.monotonic,
        cooldown: CooldownLike | None = None,
        dedupe: DedupeTracker | None = None,
    ) -> None:
        self._config = config_provider
        self._rng = rng or random.Random()
        self._clock = clock
        self._cooldown = cooldown
        self._dedupe = dedupe or DedupeTracker(clock=clock)
        self.last_decision: TriggerDecision | None = None

    # -- 主入口 ------------------------------------------------------------- #
    def evaluate(self, candidate: CandidateEvent) -> TriggerDecision:
        cfg = self._config()
        mode = cfg.mode.current
        tcfg = cfg.trigger

        if self._cooldown is not None and self._cooldown.is_active():
            return self._reject(
                mode, tcfg, "冷却中", f"冷却剩余 {self._cooldown.remaining():.0f}s"
            )

        if candidate.text and self._dedupe.is_duplicate(candidate.text):
            return self._reject(mode, tcfg, "重复语句", "同一句话在去重窗口内已处理过")

        duration = candidate.features.duration_s if candidate.features else 0.0
        if duration and duration < tcfg.min_voice_seconds:
            return self._reject(
                mode,
                tcfg,
                "语音过短",
                f"{duration:.2f}s < 最短语音 {tcfg.min_voice_seconds:.2f}s",
            )

        scores = self._collect_scores(candidate, cfg)
        if not scores:
            return self._reject(mode, tcfg, "没有可用的分析层", "关键词/规则/AI 全部关闭或缺失")

        weights = self._active_weights(scores, cfg)
        total_weight = sum(weights.values())
        combined = sum(scores[key] * weights[key] for key in scores) / total_weight
        combined = max(0.0, min(1.0, combined))

        probability, tier, eligible = self._select_probability(
            combined, cfg, ai_fallback=bool(cfg.ai.enabled and candidate.ai is None)
        )
        roll = self._rng.random()
        sampled = probability < 1.0
        trigger = eligible and roll < probability

        if not eligible:
            reason = (
                f"合成分 {combined:.2f} 低于最低置信度阈值 {tcfg.low_confidence_threshold:.2f}"
            )
            decision = self._reject(mode, tcfg, "分数不足", reason)
            decision.combined = combined
            decision.scores = scores
            decision.weighted = {key: scores[key] * weights[key] / total_weight for key in scores}
            decision.tier = tier
            return decision

        if not trigger:
            decision = self._reject(
                mode,
                tcfg,
                "概率未命中",
                f"合成分 {combined:.2f} 达标，但抽样未命中（{probability:.0%}）",
            )
            decision.combined = combined
            decision.scores = scores
            decision.weighted = {key: scores[key] * weights[key] / total_weight for key in scores}
            decision.probability = probability
            decision.sampled = True
            decision.tier = tier
            return decision

        detail = self._describe_layers(candidate, scores, weights)
        reason = f"触发（{detail}）"
        decision = TriggerDecision(
            trigger=True,
            reason=reason,
            mode=mode,
            combined=combined,
            scores=scores,
            weighted={key: scores[key] * weights[key] / total_weight for key in scores},
            probability=probability,
            sampled=sampled,
            event_id=uuid.uuid4().hex[:12],
            tier=tier,
        )
        if candidate.text:
            self._dedupe.remember(candidate.text)
        self.last_decision = decision
        log.info("触发决策：%s", decision.describe())
        return decision

    # -- 内部 --------------------------------------------------------------- #
    def _collect_scores(self, candidate: CandidateEvent, cfg: AppConfig) -> dict[str, float]:
        scores: dict[str, float] = {}
        if cfg.keywords.enabled and candidate.keyword is not None:
            scores["keyword"] = max(0.0, min(1.0, candidate.keyword.score))
        if cfg.rules.enabled and candidate.rule is not None:
            scores["rule"] = max(0.0, min(1.0, candidate.rule.score))
        if cfg.ai.enabled and candidate.ai is not None:
            scores["ai"] = max(0.0, min(1.0, candidate.ai.score))
        return scores

    def _active_weights(self, scores: dict[str, float], cfg: AppConfig) -> dict[str, float]:
        weights = {key: max(0.0, float(cfg.trigger.weights.get(key, 0.0))) for key in scores}
        total = sum(weights.values())
        if total <= 0:
            # 用户把权重全设 0 时退回均分，避免除零与「永远不触发」的迷惑行为
            return {key: 1.0 / len(scores) for key in scores}
        return {key: value / total for key, value in weights.items()}

    def _select_probability(
        self, combined: float, cfg: AppConfig, *, ai_fallback: bool
    ) -> tuple[float, str, bool]:
        """返回 (概率, 概率档位, 是否有资格触发)。

        两模式的差别只在概率策略：
        - serious：达到主阈值 → serious 概率；落在低置信度区间 → low_confidence 概率；
          再低 → 直接没有资格（这才是「认真识别」）。
        - chaotic：达到主阈值 → chaotic 概率；低于主阈值 → chaotic_low_score 概率，
          即「普通废话也有机会被整活」。
        """
        tcfg = cfg.trigger
        probs = tcfg.probabilities
        threshold = tcfg.trigger_threshold
        low_threshold = tcfg.low_confidence_threshold
        mode = cfg.mode.current

        if mode == MODE_CHAOTIC:
            if combined >= threshold:
                return max(0.0, min(1.0, probs.get("chaotic", 0.5))), "chaotic", True
            return max(0.0, min(1.0, probs.get("chaotic_low_score", 0.2))), "chaotic_low", True

        if combined >= threshold:
            if ai_fallback:
                return max(0.0, min(1.0, probs.get("ai_unavailable", 0.6))), "ai_unavailable", True
            return max(0.0, min(1.0, probs.get("serious", 1.0))), "serious", True
        if combined >= low_threshold:
            return max(0.0, min(1.0, probs.get("low_confidence", 0.35))), "low_confidence", True
        return 0.0, "below_low", False

    def _describe_layers(
        self, candidate: CandidateEvent, scores: dict[str, float], weights: dict[str, float]
    ) -> str:
        parts: list[str] = []
        if candidate.keyword is not None and candidate.keyword.hits:
            parts.append(f"关键词「{candidate.keyword.hit_text}」")
        if candidate.rule is not None:
            key, value = max(candidate.rule.components.items(), key=lambda item: item[1], default=("", 0.0))
            if key:
                parts.append(f"规则 {key}={value:.2f}")
        if candidate.ai is not None:
            parts.append(f"AI 悲壮={candidate.ai.dramatic_score:.2f}")
        if not parts:
            parts.append("综合评分达标")
        layer_text = " + ".join(
            f"{key}:{scores[key]:.2f}×{weights[key]:.2f}" for key in scores
        )
        return "；".join(parts) + f"（{layer_text}）"

    def _reject(
        self, mode: str, tcfg: object, rejected_by: str, detail: str
    ) -> TriggerDecision:
        decision = TriggerDecision(
            trigger=False,
            reason=f"未触发（{detail}）",
            mode=mode,
            rejected_by=rejected_by,
        )
        self.last_decision = decision
        log.debug("未触发：%s - %s", rejected_by, detail)
        return decision

"""规则层：把声学特征折算成 0~1 的辅助分数。

这里的每一个分量都**不能单独构成触发理由**（提示词明确要求：大喊、哭腔、长时间
沉默都不能直接算悲壮）。规则层只回答「这段话在声学上像不像一句准备赴死的话」，
最终由 TriggerEngine 与关键词/AI 一起加权。

分量语义：
- speech_duration   连续说话够不够长（短促喊叫得分低）
- pause_after       句中出现过长停顿（哽咽、说不下去）
- volume_drop       尾部音量相对峰值明显衰减（越说越小声）
- volume_spike      句中出现突然拔高（情绪爆点）
- silence_after     尾部非常安静（话没说完/沉默收尾）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..config.config_manager import RulesConfig
from .vad import UtteranceFeatures

log = logging.getLogger(__name__)


@dataclass
class RuleResult:
    score: float
    components: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def dominant(self, top: int = 2) -> list[tuple[str, float]]:
        return sorted(self.components.items(), key=lambda item: -item[1])[:top]

    def describe(self) -> str:
        if not self.components:
            return "规则层未启用"
        key, value = max(self.components.items(), key=lambda item: item[1])
        label = COMPONENT_LABELS.get(key, key)
        return f"规则：{label} {value:.2f}（总分 {self.score:.2f}）"


COMPONENT_LABELS = {
    "speech_duration": "说话够长",
    "pause_after": "句内长停顿",
    "volume_drop": "越说越小声",
    "volume_spike": "突然拔高",
    "silence_after": "沉默收尾",
}


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


class RuleAnalyzer:
    def __init__(self, config: RulesConfig) -> None:
        self.config = config

    def update_config(self, config: RulesConfig) -> None:
        self.config = config

    def analyze(self, features: UtteranceFeatures) -> RuleResult:
        cfg = self.config
        if not cfg.enabled:
            return RuleResult(score=0.0, components={}, notes=["规则层已关闭"])

        full_duration = max(0.5, cfg.speech_duration_full_score_s)
        long_pause = max(0.1, cfg.long_pause_s)
        floor_db = -60.0

        dynamic_range = max(1.0, features.peak_db - floor_db)
        components = {
            "speech_duration": _clamp(features.duration_s / full_duration),
            "pause_after": _clamp(features.longest_pause_s / long_pause),
            "volume_drop": _clamp(features.volume_drop_ratio),
            "volume_spike": _clamp(features.volume_spike_db / 12.0),
            "silence_after": _clamp(1.0 - (features.tail_db - floor_db) / dynamic_range),
        }

        weights = {key: float(cfg.weights.get(key, 0.0)) for key in components}
        total_weight = sum(weights.values())
        if total_weight <= 0:
            return RuleResult(score=0.0, components=components, notes=["规则权重全为 0"])

        score = sum(components[key] * weights[key] for key in components) / total_weight
        notes: list[str] = []
        if features.voiced_ratio < 0.3:
            notes.append("语音占比偏低（可能只是噪声）")
        if features.duration_s < 0.5:
            notes.append("语音过短")
        if features.peak_db < floor_db + 6:
            notes.append("音量过低")

        return RuleResult(
            score=_clamp(score),
            components={key: round(value, 4) for key, value in components.items()},
            notes=notes,
        )

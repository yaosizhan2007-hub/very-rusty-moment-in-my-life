"""语义分析层（EmotionAnalyzer）接口与结构化结果校验。

阶段 2 只提供接口 + 空实现：程序在「没有 AI」时完全可用（关键词+规则）。
阶段 3 会在这里补 OpenAI 兼容后端与本地模型后端，届时不需要改动
TriggerEngine 与 GUI。

铁律：
- AI 只输出分数，**不能**直接控制音频设备；
- 返回值必须做类型/范围校验，超时/非法数据一律视为「AI 不可用」，
  由 TriggerEngine 重新归一化权重回退到关键词+规则。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

log = logging.getLogger(__name__)


class AnalysisError(Exception):
    """AI 分析失败（超时、非法返回、网络错误）。"""


class AnalysisUnavailable(AnalysisError):
    """AI 功能未启用或不可用。"""


@dataclass(frozen=True)
class EmotionScores:
    dramatic_score: float
    context_score: float
    comedic_potential: float
    reason: str
    confidence: float
    corrected_text: str = ""
    """AI 按上下文纠正后的句子（语音识别常出现同音字错误）；为空表示无需纠正。"""
    transcript_quality: float = 1.0
    """这段转写像不像可信的中文语句（0=噪声/幻觉，1=清晰完整）。"""

    @property
    def score(self) -> float:
        """合成给 TriggerEngine 用的单值分数（0~1）。

        取悲壮与上下文的主干，再用置信度做温和缩放：置信度低的判断不该
        和「非常确定」拿到同样的权重。
        """
        base = 0.7 * _clamp(self.dramatic_score) + 0.3 * _clamp(self.context_score)
        return _clamp(base * max(0.2, _clamp(self.confidence)))

    def describe(self) -> str:
        extra = ""
        if self.corrected_text:
            extra += f"，纠正为「{self.corrected_text}」"
        if self.transcript_quality < 0.5:
            extra += f"，转写可信度 {self.transcript_quality:.2f}"
        return (
            f"AI：悲壮 {self.dramatic_score:.2f} / 上下文 {self.context_score:.2f} / "
            f"喜剧潜力 {self.comedic_potential:.2f} / 置信 {self.confidence:.2f}"
            + (f"（{self.reason}）" if self.reason else "")
            + extra
        )


def _clamp(value: Any, low: float = 0.0, high: float = 1.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return low
    if number != number:  # NaN
        return low
    return max(low, min(high, number))


REQUIRED_FIELDS = ("dramatic_score", "context_score", "comedic_potential", "confidence")


def _normalize_scale(values: list[float]) -> list[float]:
    """模型经常用 1~10 分制（真实遇到过 1/3/2 这种输出）。

    只要有任何一个分数 > 1，就认为整体是 10 分制并统一除以 10；宁可把
    「悲壮」估低一点，也不要因为 3 被 clamp 成 1.0 而乱切音乐。
    """
    if any(value > 1.0 for value in values):
        return [value / 10.0 for value in values]
    return values


def parse_scores(payload: dict[str, Any]) -> EmotionScores:
    """校验并转换 AI 返回的结构化结果，非法数据抛 :class:`AnalysisError`。"""
    if not isinstance(payload, dict):
        raise AnalysisError("AI 返回的不是 JSON 对象")
    missing = [key for key in REQUIRED_FIELDS if key not in payload]
    if missing:
        raise AnalysisError(f"AI 返回缺少字段：{', '.join(missing)}")
    reason = payload.get("reason", "")
    if not isinstance(reason, str):
        reason = str(reason)

    raw_values = [
        _clamp(payload["dramatic_score"], 0.0, 100.0),
        _clamp(payload["context_score"], 0.0, 100.0),
        _clamp(payload["comedic_potential"], 0.0, 100.0),
        _clamp(payload["confidence"], 0.0, 100.0),
    ]
    dramatic, context, comedic, confidence = _normalize_scale(raw_values)

    corrected = payload.get("corrected_text", "")
    if not isinstance(corrected, str):
        corrected = ""
    corrected = corrected.strip()
    if corrected and len(corrected) > 120:  # 模型偶尔会写解释，截断保护
        corrected = corrected[:120]

    quality_raw = _clamp(payload.get("transcript_quality", 1.0), 0.0, 100.0)
    quality = quality_raw / 10.0 if quality_raw > 1.0 else quality_raw

    return EmotionScores(
        dramatic_score=_clamp(dramatic),
        context_score=_clamp(context),
        comedic_potential=_clamp(comedic),
        reason=reason[:200],
        confidence=_clamp(confidence),
        corrected_text=corrected,
        transcript_quality=_clamp(quality),
    )


class EmotionAnalyzer(Protocol):
    """可替换的语义分析接口。实现方必须自己处理超时（或在 timeout 参数内完成）。"""

    name: str

    @property
    def available(self) -> bool: ...

    def analyze(self, text: str, context: list[str] | None = None) -> EmotionScores: ...


class NullAnalyzer:
    """未启用 AI 时的空实现：显式不可用，而不是伪装成功。"""

    name = "none"

    @property
    def available(self) -> bool:
        return False

    def analyze(self, text: str, context: list[str] | None = None) -> EmotionScores:
        raise AnalysisUnavailable("未启用语义分析（AI 层已关闭）")

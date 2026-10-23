"""组合规则引擎：多条件同时成立时的评分修正。

条件语义（对应规格书第四节）：
- ``requires``：全部满足；
- ``any_of``：至少满足一个（留空表示无此限制）；
- ``excludes``：任一命中则整条规则不生效。

每条规则输出一个 score_delta（可为负），最终所有命中规则的 delta 相加，
由 TriggerEngine 加到合成分上并裁剪到 0~1。规则只做「加减分」，不做否决——
否决逻辑仍然只有冷却、去重、全局静音与用户手动开关。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .context import ContextTracker
from .matcher import PhraseResult
from .models import CombinationRule, PhraseLibrary, RuleCondition

log = logging.getLogger(__name__)


@dataclass
class RuleMatch:
    rule_id: str
    name: str
    delta: float
    reasons: list[str] = field(default_factory=list)

    def describe(self) -> str:
        detail = "、".join(self.reasons) if self.reasons else "条件成立"
        return f"{self.name}（{detail}，{self.delta:+.2f}）"


@dataclass
class RuleOutcome:
    matches: list[RuleMatch] = field(default_factory=list)
    total_delta: float = 0.0

    @property
    def applied(self) -> bool:
        return bool(self.matches)

    def describe(self) -> str:
        if not self.matches:
            return "无组合规则命中"
        return "；".join(match.describe() for match in self.matches)


class RuleEngine:
    """按词库里定义的组合规则打分（无状态，线程安全）。"""

    def __init__(self, library: PhraseLibrary) -> None:
        self.library = library

    def reload(self, library: PhraseLibrary) -> None:
        self.library = library

    # -- 求值 --------------------------------------------------------------- #
    def evaluate(
        self,
        *,
        mode: str,
        phrase: PhraseResult,
        context: ContextTracker | None = None,
        speech_confidence: float | None = None,
    ) -> RuleOutcome:
        outcome = RuleOutcome()
        for rule in self.library.rules:
            if not rule.enabled or not rule.applies_to(mode):
                continue
            if speech_confidence is not None and speech_confidence < rule.min_confidence:
                continue
            if not self._all_satisfied(rule.requires, rule, phrase, context):
                continue
            if rule.any_of and not any(
                self._satisfied(condition, rule, phrase, context) for condition in rule.any_of
            ):
                continue
            if any(self._satisfied(condition, rule, phrase, context) for condition in rule.excludes):
                continue
            reasons = [self._describe_condition(cond) for cond in rule.requires]
            if rule.any_of:
                hit = next(
                    (cond for cond in rule.any_of if self._satisfied(cond, rule, phrase, context)),
                    None,
                )
                if hit is not None:
                    reasons.append(self._describe_condition(hit))
            outcome.matches.append(
                RuleMatch(rule_id=rule.id, name=rule.name, delta=rule.score_delta, reasons=reasons)
            )

        outcome.total_delta = sum(match.delta for match in outcome.matches)
        if outcome.matches:
            log.debug("组合规则：%s（合计 %+.2f）", outcome.describe(), outcome.total_delta)
        return outcome

    # -- 条件 --------------------------------------------------------------- #
    def _all_satisfied(
        self,
        conditions: list[RuleCondition],
        rule: CombinationRule,
        phrase: PhraseResult,
        context: ContextTracker | None,
    ) -> bool:
        return all(self._satisfied(cond, rule, phrase, context) for cond in conditions)

    def _satisfied(
        self,
        condition: RuleCondition,
        rule: CombinationRule,
        phrase: PhraseResult,
        context: ContextTracker | None,
    ) -> bool:
        window = rule.window_seconds
        kind = condition.kind
        scope = condition.scope or "current"
        use_current = scope in ("current", "any")
        use_context = scope in ("context", "any") and context is not None

        if kind == "category":
            if use_current and (
                condition.value in phrase.categories
                or condition.value in phrase.exclusion_categories
            ):
                return True
            return bool(use_context and context.has_category(condition.value, window))
        if kind == "entry":
            if use_current and any(hit.entry_id == condition.value for hit in phrase.hits):
                return True
            return bool(use_context and context.has_entry(condition.value, window))
        if kind == "tag":
            if use_current and condition.value in phrase.tags:
                return True
            return bool(use_context and context.has_tag(condition.value, window))
        if kind == "score_at_least":
            return use_current and phrase.score >= condition.threshold
        log.debug("未知规则条件 kind=%s（规则 %s），按不满足处理", kind, rule.id)
        return False

    @staticmethod
    def _describe_condition(condition: RuleCondition) -> str:
        labels = {
            "category": "类别",
            "entry": "词条",
            "tag": "标签",
            "score_at_least": "分数≥",
        }
        label = labels.get(condition.kind, condition.kind)
        if condition.kind == "score_at_least":
            return f"{label}{condition.threshold:.2f}"
        return f"{label}:{condition.value}"

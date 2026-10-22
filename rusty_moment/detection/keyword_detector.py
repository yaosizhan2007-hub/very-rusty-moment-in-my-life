"""关键词层：正向触发词 + 反向排除词 + 权重 + 最小值命中数。

设计上坚持「关键词不能直接保证触发」：
- 正向词只给出 0~1 的分数，最终是否触发由 TriggerEngine 结合规则/AI 与阈值决定；
- 排除词不是硬否决，而是按权重打折——「别管我」本身可能只是开玩笑，
  要允许它和语音时长、音量衰减等证据一起决定。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..config.config_manager import KeywordsConfig
from .text_normalize import normalize_for_matching

log = logging.getLogger(__name__)

# 单个满权重正向词即可达到的分值上限
FULL_SCORE_WEIGHT = 1.0
EXCLUSION_PENALTY_FACTOR = 0.8
MAX_EXCLUSION_PENALTY = 0.9


@dataclass(frozen=True)
class KeywordHit:
    phrase: str
    weight: float
    start: int
    end: int
    matched: str = ""
    """实际命中的文本；配了近义词时可能与 phrase 不同。"""

    @property
    def display(self) -> str:
        if self.matched and self.matched != self.phrase:
            return f"{self.matched}（≈{self.phrase}）"
        return self.phrase


@dataclass
class KeywordResult:
    score: float
    hits: list[KeywordHit] = field(default_factory=list)
    exclusions: list[KeywordHit] = field(default_factory=list)
    matched: bool = False

    @property
    def hit_text(self) -> str:
        return "、".join(hit.display for hit in self.hits)

    def describe(self) -> str:
        parts = []
        if self.hits:
            parts.append("命中：" + self.hit_text)
        if self.exclusions:
            parts.append("排除词：" + "、".join(item.display for item in self.exclusions))
        return "；".join(parts) if parts else "未命中关键词"


class KeywordDetector:
    """无状态匹配器（线程安全）。"""

    def __init__(self, config: KeywordsConfig) -> None:
        self.config = config

    def update_config(self, config: KeywordsConfig) -> None:
        self.config = config

    def analyze(self, text: str) -> KeywordResult:
        cfg = self.config
        if not cfg.enabled or not text:
            return KeywordResult(score=0.0, matched=False)

        haystack = normalize_for_matching(
            text, simplify=True, lower=not cfg.case_sensitive
        )
        hits = self._find_all(haystack, cfg.positives)
        exclusions = self._find_all(haystack, cfg.exclusions)

        positive = min(1.0, sum(hit.weight for hit in hits) / FULL_SCORE_WEIGHT)
        penalty = min(MAX_EXCLUSION_PENALTY, sum(item.weight for item in exclusions) * EXCLUSION_PENALTY_FACTOR)
        score = max(0.0, positive * (1.0 - penalty))
        matched = len(hits) >= cfg.min_hits and score > 0.0
        return KeywordResult(score=score, hits=hits, exclusions=exclusions, matched=matched)

    def _find_all(self, haystack: str, phrases: list[dict[str, object]]) -> list[KeywordHit]:
        """在归一化后的文本里找主词与它的近义说法；同一词条最多计一次。"""
        found: list[KeywordHit] = []
        lower = not self.config.case_sensitive
        for item in phrases:
            phrase = str(item.get("phrase", ""))
            if not phrase:
                continue
            weight = float(item.get("weight", 1.0) or 1.0)
            raw_aliases = item.get("aliases") or []
            candidates = [phrase]
            if isinstance(raw_aliases, (list, tuple)):
                candidates.extend(str(alias) for alias in raw_aliases if str(alias).strip())
            best_index = -1
            best_len = 0
            best_text = ""
            for candidate in candidates:
                needle = normalize_for_matching(candidate, simplify=True, lower=lower)
                if not needle:
                    continue
                index = haystack.find(needle)
                if index < 0:
                    continue
                if best_index < 0 or index < best_index:
                    best_index, best_len, best_text = index, len(needle), candidate
            if best_index < 0:
                continue
            found.append(
                KeywordHit(
                    phrase=phrase,
                    weight=weight,
                    start=best_index,
                    end=best_index + best_len,
                    matched=best_text,
                )
            )
        # 权重高的排前面，方便日志里看出主导词
        found.sort(key=lambda hit: (-hit.weight, hit.start))
        return found

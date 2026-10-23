"""短语匹配器：把一句话映射成结构化命中结果（类别 / 权重 / 上下文标签 / 匹配方式）。

设计要点（对应规格书第三、八节）：
- 精确匹配优先：先做规范化（繁转简、标点、空白），再按规范表达 + 变体做子串匹配；
- 只在精确失败时才尝试模糊匹配，且有长度与相似度门槛，避免「别管我」和「别别管我」被混为一谈；
- 排除类词条不打分，只按权重打折（沿用 v1 的设计：排除不是硬否决）；
- 每次命中都记录匹配方式与相似度，方便 GUI 的「为什么命中」解释。
"""

from __future__ import annotations

import difflib
import logging
from dataclasses import dataclass, field

from .models import PhraseEntry, PhraseLibrary
from ..detection.text_normalize import normalize_for_matching

log = logging.getLogger(__name__)

EXCLUSION_CATEGORY = "exclusion"
EXCLUSION_PENALTY_FACTOR = 0.8
MAX_EXCLUSION_PENALTY = 0.9
FULL_SCORE_WEIGHT = 1.0

# 模糊匹配只在短文本上做，避免长句做 O(n·m) 相似度计算拖慢流水线
FUZZY_MAX_TEXT_LEN = 24
FUZZY_MIN_ENTRY_LEN = 4


@dataclass(frozen=True)
class PhraseHit:
    entry_id: str
    canonical: str
    category: str
    weight: float
    matched_text: str
    method: str = "exact"
    """exact（规范表达或变体直接命中）、fuzzy（相似度命中）或 keyword（短关键词命中）。"""
    similarity: float = 1.0
    start: int = -1
    length: int = 0

    @property
    def end(self) -> int:
        return self.start + self.length if self.start >= 0 else -1

    @property
    def display(self) -> str:
        if self.matched_text and self.matched_text != self.canonical:
            return f"{self.matched_text}（≈{self.canonical}）"
        return self.canonical


@dataclass
class PhraseResult:
    """一句话的匹配结果；字段与 v1 的 KeywordResult 保持兼容（score/hits/matched）。"""

    score: float = 0.0
    hits: list[PhraseHit] = field(default_factory=list)
    exclusions: list[PhraseHit] = field(default_factory=list)
    matched: bool = False
    categories: list[str] = field(default_factory=list)
    exclusion_categories: list[str] = field(default_factory=list)
    """排除类命中的类别（通常只有 exclusion，留给组合规则判断「是否玩笑/引用」）。"""
    tags: list[str] = field(default_factory=list)
    comedic_potential: float = 0.0
    fuzzy_hits: int = 0

    @property
    def hit_text(self) -> str:
        return "、".join(hit.display for hit in self.hits)

    def describe(self) -> str:
        parts = []
        if self.hits:
            parts.append("命中：" + self.hit_text)
        if self.exclusions:
            parts.append("排除词：" + "、".join(item.display for item in self.exclusions))
        if self.fuzzy_hits:
            parts.append(f"模糊匹配 {self.fuzzy_hits} 项")
        return "；".join(parts) if parts else "未命中词库"


def _dedupe_overlaps(hits: list[PhraseHit]) -> list[PhraseHit]:
    """同一段文本被多个表达命中时，只保留最长（再按权重）的那个。

    没有这一步，「你们先走，我断后」会同时命中长句词条和「你们走」这种短关键词，
    分数被重复累加；反过来单独说「你们走」时又只能靠短关键词命中。
    """
    chosen: list[PhraseHit] = []
    for hit in sorted(hits, key=lambda item: (-item.length, -item.weight, item.start)):
        if hit.start < 0:
            chosen.append(hit)
            continue
        overlapped = any(
            hit.start < other.end and other.start < hit.end
            for other in chosen
            if other.start >= 0
        )
        if not overlapped:
            chosen.append(hit)
    return chosen


class PhraseMatcher:
    """无状态匹配器（线程安全）；词库更新时整体替换实例即可。"""

    def __init__(
        self,
        library: PhraseLibrary,
        *,
        case_sensitive: bool = False,
        min_hits: int = 1,
        fuzzy: bool | None = None,
    ) -> None:
        self.library = library
        self.case_sensitive = case_sensitive
        self.min_hits = max(1, int(min_hits))
        settings = library.settings
        self.fuzzy_enabled = settings.fuzzy_enabled if fuzzy is None else bool(fuzzy)
        self.fuzzy_min_similarity = settings.fuzzy_min_similarity
        self._index = self._build_index(library)

    # -- 索引 --------------------------------------------------------------- #
    def _build_index(self, library: PhraseLibrary) -> list[tuple[str, str, PhraseEntry, bool]]:
        """(归一化后的表达, 原始表达, 词条, 是否排除类)。"""
        index: list[tuple[str, str, PhraseEntry, bool]] = []
        lower = not self.case_sensitive
        for entry in library.entries:
            is_exclusion = entry.category == EXCLUSION_CATEGORY
            for form in entry.all_forms():
                needle = normalize_for_matching(form, simplify=True, lower=lower)
                if needle:
                    index.append((needle, form, entry, is_exclusion))
        return index

    def reload(self, library: PhraseLibrary) -> None:
        self.library = library
        self._index = self._build_index(library)

    # -- 匹配 --------------------------------------------------------------- #
    def analyze(
        self,
        text: str,
        *,
        mode: str | None = None,
        speech_confidence: float | None = None,
    ) -> PhraseResult:
        result = PhraseResult()
        if not text:
            return result

        haystack = normalize_for_matching(text, simplify=True, lower=not self.case_sensitive)
        if not haystack:
            return result

        best: dict[str, PhraseHit] = {}
        exclusions: dict[str, PhraseHit] = {}
        lower = not self.case_sensitive

        for needle, form, entry, is_exclusion in self._index:
            if mode is not None and not entry.applies_to(mode):
                continue
            index = haystack.find(needle)
            if index < 0:
                continue
            if speech_confidence is not None and speech_confidence < entry.min_confidence:
                continue
            hit = PhraseHit(
                entry_id=entry.id,
                canonical=entry.canonical_phrase,
                category=entry.category,
                weight=entry.weight,
                matched_text=form,
                method="keyword" if entry.match_type == "keyword" else "exact",
                start=index,
                length=len(needle),
            )
            target = exclusions if is_exclusion else best
            current = target.get(entry.id)
            # 同一词条只计一次，优先保留更靠前（更早出现）的命中
            if current is None or index < current.start:
                target[entry.id] = hit

        hits = list(best.values())
        fuzzy_count = 0
        if not hits and self.fuzzy_enabled and 0 < len(haystack) <= FUZZY_MAX_TEXT_LEN:
            hits, fuzzy_count = self._fuzzy_match(haystack, mode, speech_confidence, lower)
        hits = _dedupe_overlaps(hits)

        exclusions_list = list(exclusions.values())
        result.hits = sorted(hits, key=lambda hit: (-hit.weight, hit.start))
        result.exclusions = sorted(exclusions_list, key=lambda hit: (-hit.weight, hit.start))
        result.fuzzy_hits = fuzzy_count

        positive = min(1.0, sum(hit.weight for hit in result.hits) / FULL_SCORE_WEIGHT)
        penalty = min(
            MAX_EXCLUSION_PENALTY,
            sum(item.weight for item in result.exclusions) * EXCLUSION_PENALTY_FACTOR,
        )
        result.score = max(0.0, positive * (1.0 - penalty))
        result.matched = len(result.hits) >= self.min_hits and result.score > 0.0

        result.categories = sorted({hit.category for hit in result.hits})
        result.exclusion_categories = sorted({hit.category for hit in result.exclusions})
        tags: set[str] = set()
        for hit in result.hits:
            entry = self.library.by_id(hit.entry_id)
            if entry is not None:
                tags.update(entry.context_tags)
        result.tags = sorted(tags)
        if result.hits:
            comedic = []
            for hit in result.hits:
                entry = self.library.by_id(hit.entry_id)
                if entry is not None:
                    comedic.append(entry.comedic_potential)
            result.comedic_potential = sum(comedic) / len(comedic) if comedic else 0.0
        return result

    # -- 模糊匹配 ----------------------------------------------------------- #
    def _fuzzy_match(
        self,
        haystack: str,
        mode: str | None,
        speech_confidence: float | None,
        lower: bool,
    ) -> tuple[list[PhraseHit], int]:
        """精确匹配落空时，对长度接近的表达做相似度匹配。

        只看长度差不超过 40% 的候选，并且要求相似度达到阈值——
        宁可漏判，也不要因为一个字之差把语义相反的句子算成命中。
        """
        matches: dict[str, PhraseHit] = {}
        text_len = len(haystack)
        for needle, form, entry, is_exclusion in self._index:
            if is_exclusion or entry.category == EXCLUSION_CATEGORY:
                continue
            if mode is not None and not entry.applies_to(mode):
                continue
            if speech_confidence is not None and speech_confidence < entry.min_confidence:
                continue
            if abs(len(needle) - text_len) > max(2, int(text_len * 0.4)):
                continue
            ratio = difflib.SequenceMatcher(None, haystack, needle).ratio()
            if ratio < self.fuzzy_min_similarity:
                continue
            current = matches.get(entry.id)
            if current is None or ratio > current.similarity:
                matches[entry.id] = PhraseHit(
                    entry_id=entry.id,
                    canonical=entry.canonical_phrase,
                    category=entry.category,
                    weight=entry.weight * min(1.0, ratio),
                    matched_text=haystack,
                    method="fuzzy",
                    similarity=ratio,
                    start=0,
                )
        hits = list(matches.values())
        log.debug("模糊匹配命中 %d 条（阈值 %.2f）", len(hits), self.fuzzy_min_similarity)
        return hits, len(hits)

    # -- 诊断 --------------------------------------------------------------- #
    def explain(self, text: str, *, mode: str | None = None) -> dict[str, object]:
        """给 GUI「测试这句话」用的结构化解释。"""
        result = self.analyze(text, mode=mode)
        return {
            "text": text,
            "mode": mode or "(未指定)",
            "score": round(result.score, 3),
            "matched": result.matched,
            "hits": [
                {
                    "id": hit.entry_id,
                    "canonical": hit.canonical,
                    "matched": hit.matched_text,
                    "category": hit.category,
                    "weight": round(hit.weight, 3),
                    "method": hit.method,
                    "similarity": round(hit.similarity, 3),
                }
                for hit in result.hits
            ],
            "exclusions": [
                {
                    "id": hit.entry_id,
                    "canonical": hit.canonical,
                    "matched": hit.matched_text,
                    "weight": round(hit.weight, 3),
                }
                for hit in result.exclusions
            ],
            "categories": result.categories,
            "tags": result.tags,
            "comedic_potential": round(result.comedic_potential, 3),
        }

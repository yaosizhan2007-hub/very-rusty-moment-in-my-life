"""词库数据模型：结构化词条、类别元信息与词库容器。

设计原则（对应规格书第二、六节）：
- 每个词条都带可编程使用的元数据，不是裸字符串；
- 规范表达（canonical_phrase）与同义变体（variants）分开存放，便于去重与统一修改；
- 类别是有限枚举，权重/模式/最低置信度都有明确范围；
- 未知字段保留在 extra 里，保证向前兼容（新版写回旧版程序不丢数据）。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

SCHEMA_VERSION = 1

# --------------------------------------------------------------------------- #
# 类别
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CategoryMeta:
    key: str
    label: str
    """中文显示名（GUI 用）。"""
    description: str
    default_weight: float
    default_modes: tuple[str, ...]
    risk: str
    """误触发风险：low / medium / high（高风险的类别需要更高置信度）。"""


CATEGORIES: tuple[CategoryMeta, ...] = (
    CategoryMeta(
        key="sacrifice",
        label="牺牲与掩护",
        description="断后、掩护队友、主动换命",
        default_weight=0.95,
        default_modes=("serious", "chaos"),
        risk="low",
    ),
    CategoryMeta(
        key="farewell",
        label="告别与托付",
        description="遗言式告别、交代后事、托付",
        default_weight=0.90,
        default_modes=("serious", "chaos"),
        risk="low",
    ),
    CategoryMeta(
        key="defeat",
        label="战败与绝望",
        description="认输、放弃、撑不住",
        default_weight=0.85,
        default_modes=("serious", "chaos"),
        risk="medium",
    ),
    CategoryMeta(
        key="teammate",
        label="队友互动",
        description="对队友说的话：你先走、把装备拿走",
        default_weight=0.80,
        default_modes=("serious", "chaos"),
        risk="medium",
    ),
    CategoryMeta(
        key="daily",
        label="日常废话与反差整活",
        description="喝水、外卖、鼠标没电——整活模式的主料",
        default_weight=0.35,
        default_modes=("chaos",),
        risk="high",
    ),
    CategoryMeta(
        key="transition",
        label="情绪转折与语境线索",
        description="算了、就这样吧——依赖上下文，不单独定案",
        default_weight=0.55,
        default_modes=("serious", "chaos"),
        risk="high",
    ),
    CategoryMeta(
        key="jargon",
        label="游戏黑话与口语变体",
        description="我倒了、没甲了、这波寄了",
        default_weight=0.70,
        default_modes=("serious", "chaos"),
        risk="medium",
    ),
    CategoryMeta(
        key="exclusion",
        label="排除与抑制",
        description="玩笑、引用、战术报点——按权重打折而非硬否决",
        default_weight=0.80,
        default_modes=("serious", "chaos"),
        risk="low",
    ),
)

CATEGORY_KEYS: tuple[str, ...] = tuple(meta.key for meta in CATEGORIES)
CATEGORY_BY_KEY: dict[str, CategoryMeta] = {meta.key: meta for meta in CATEGORIES}

MODES: tuple[str, ...] = ("serious", "chaos")
MATCH_TYPES: tuple[str, ...] = ("phrase", "keyword", "fuzzy")
SOURCES: tuple[str, ...] = ("curated", "user", "ai_candidate", "legacy")

# 常见上下文标签（不强制枚举，未知标签只警告不拒绝）
KNOWN_CONTEXT_TAGS: tuple[str, ...] = (
    "teammate",
    "combat",
    "farewell",
    "death",
    "low_hp",
    "objective",
    "chat",
    "joke",
    "quote",
    "off_topic",
)


# --------------------------------------------------------------------------- #
# 词条
# --------------------------------------------------------------------------- #


@dataclass
class PhraseEntry:
    """一条可匹配的表达及其元数据。"""

    id: str
    canonical_phrase: str
    variants: list[str] = field(default_factory=list)
    category: str = "sacrifice"
    weight: float = 0.8
    match_type: str = "phrase"
    modes: list[str] = field(default_factory=lambda: ["serious", "chaos"])
    context_tags: list[str] = field(default_factory=list)
    negative_contexts: list[str] = field(default_factory=list)
    min_confidence: float = 0.0
    comedic_potential: float = 0.4
    enabled: bool = True
    source: str = "curated"
    extra: dict[str, Any] = field(default_factory=dict)

    # -- 查询 --------------------------------------------------------------- #
    @property
    def display(self) -> str:
        return self.canonical_phrase

    def all_forms(self) -> list[str]:
        """规范表达 + 所有变体（去重、去空白）。"""
        out: list[str] = []
        seen: set[str] = set()
        for text in [self.canonical_phrase, *self.variants]:
            item = (text or "").strip()
            if item and item not in seen:
                seen.add(item)
                out.append(item)
        return out

    def applies_to(self, mode: str) -> bool:
        return mode in self.modes

    def clone(self) -> PhraseEntry:
        return copy.deepcopy(self)


# --------------------------------------------------------------------------- #
# 组合规则
# --------------------------------------------------------------------------- #


@dataclass
class RuleCondition:
    """单条规则条件。

    kind 取值：
    - ``category``：命中某类别（value 为类别 key）
    - ``entry``：命中某个词条 id（value 为词条 id）
    - ``tag``：上下文标签出现（value 为标签名）
    - ``score_at_least``：某维度分数达到阈值（value 为浮点）

    scope 决定证据从哪里取：
    - ``current``：只看当前这句话（默认，规则的主要依据）
    - ``context``：只看时间窗口内的历史句子（用于「上下文反转」这类跨句规则）
    - ``any``：当前句或历史任一命中即算
    """

    kind: str
    value: str = ""
    threshold: float = 0.0
    scope: str = "current"


@dataclass
class CombinationRule:
    """组合规则：多个条件同时成立时的评分调整。"""

    id: str
    name: str
    requires: list[RuleCondition] = field(default_factory=list)
    any_of: list[RuleCondition] = field(default_factory=list)
    excludes: list[RuleCondition] = field(default_factory=list)
    window_seconds: float = 8.0
    score_delta: float = 0.0
    modes: list[str] = field(default_factory=lambda: ["serious", "chaos"])
    min_confidence: float = 0.0
    enabled: bool = True
    extra: dict[str, Any] = field(default_factory=dict)

    def applies_to(self, mode: str) -> bool:
        return mode in self.modes


# --------------------------------------------------------------------------- #
# 词库
# --------------------------------------------------------------------------- #


@dataclass
class LibrarySettings:
    deduplicate_variants: bool = True
    normalize_chinese_punctuation: bool = True
    default_mode: str = "serious"
    fuzzy_enabled: bool = True
    fuzzy_min_similarity: float = 0.82
    """模糊匹配的最低相似度（仅在精确匹配失败时尝试）。"""


@dataclass
class PhraseLibrary:
    schema_version: int = SCHEMA_VERSION
    library_name: str = "Rusty Moment Core Library"
    language: list[str] = field(default_factory=lambda: ["zh-CN"])
    entries: list[PhraseEntry] = field(default_factory=list)
    rules: list[CombinationRule] = field(default_factory=list)
    settings: LibrarySettings = field(default_factory=LibrarySettings)

    # -- 查询 --------------------------------------------------------------- #
    def by_id(self, entry_id: str) -> PhraseEntry | None:
        for entry in self.entries:
            if entry.id == entry_id:
                return entry
        return None

    def in_category(self, category: str) -> list[PhraseEntry]:
        return [entry for entry in self.entries if entry.category == category]

    def enabled_entries(self, mode: str | None = None) -> list[PhraseEntry]:
        out = [entry for entry in self.entries if entry.enabled]
        if mode is not None:
            out = [entry for entry in out if entry.applies_to(mode)]
        return out

    def category_counts(self) -> dict[str, int]:
        counts = {key: 0 for key in CATEGORY_KEYS}
        for entry in self.entries:
            counts[entry.category] = counts.get(entry.category, 0) + 1
        return counts

    def variant_total(self) -> int:
        return sum(len(entry.variants) for entry in self.entries)

    # -- 变更 --------------------------------------------------------------- #
    def add_entries(self, entries: list[PhraseEntry]) -> int:
        """按 id 覆盖式合并，返回新增（不含覆盖）的条数。"""
        existing = {entry.id: index for index, entry in enumerate(self.entries)}
        added = 0
        for entry in entries:
            index = existing.get(entry.id)
            if index is None:
                self.entries.append(entry)
                existing[entry.id] = len(self.entries) - 1
            else:
                self.entries[index] = entry
        return added

    def remove_entry(self, entry_id: str) -> bool:
        for index, entry in enumerate(self.entries):
            if entry.id == entry_id:
                del self.entries[index]
                return True
        return False

    def clone(self) -> PhraseLibrary:
        return copy.deepcopy(self)


# --------------------------------------------------------------------------- #
# 摘要
# --------------------------------------------------------------------------- #


def summarize(library: PhraseLibrary) -> str:
    """一行摘要，用于日志与 GUI 状态栏。"""
    counts = library.category_counts()
    parts = [
        f"{CATEGORY_BY_KEY[key].label} {counts.get(key, 0)}"
        for key in CATEGORY_KEYS
        if counts.get(key, 0)
    ]
    return (
        f"{library.library_name}：{len(library.entries)} 条核心表达、"
        f"{library.variant_total()} 个变体、{len(library.rules)} 条组合规则"
        + ("（" + "，".join(parts) + "）" if parts else "")
    )

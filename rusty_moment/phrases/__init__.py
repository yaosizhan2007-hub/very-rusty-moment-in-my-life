"""结构化词库子系统。

分层：
- ``models``  ：数据模型（词条 / 类别 / 组合规则 / 词库容器）
- ``schema``  ：手写校验与序列化
- ``store``   ：加载、原子保存、内置词库、旧配置迁移、合并、CSV 导出
- ``matcher`` ：文本规范化 + 精确/模糊匹配（阶段 3）
- ``combination``：组合规则引擎（阶段 3）
- ``context`` ：时间窗口上下文跟踪（阶段 3）
"""

from __future__ import annotations

from .models import (
    CATEGORIES,
    CATEGORY_BY_KEY,
    CATEGORY_KEYS,
    SCHEMA_VERSION,
    CategoryMeta,
    CombinationRule,
    LibrarySettings,
    PhraseEntry,
    PhraseLibrary,
    RuleCondition,
    summarize,
)
from .schema import ValidationReport, library_to_dict, parse_library
from .store import (
    builtin_library_path,
    builtin_rules_path,
    export_csv,
    library_from_legacy_keywords,
    load_builtin,
    load_library,
    load_or_default,
    merge_libraries,
    save_library,
)

__all__ = [
    "CATEGORIES",
    "CATEGORY_BY_KEY",
    "CATEGORY_KEYS",
    "SCHEMA_VERSION",
    "CategoryMeta",
    "CombinationRule",
    "LibrarySettings",
    "PhraseEntry",
    "PhraseLibrary",
    "RuleCondition",
    "ValidationReport",
    "builtin_library_path",
    "builtin_rules_path",
    "export_csv",
    "library_from_legacy_keywords",
    "library_to_dict",
    "load_builtin",
    "load_library",
    "load_or_default",
    "merge_libraries",
    "parse_library",
    "save_library",
    "summarize",
]

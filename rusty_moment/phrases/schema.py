"""词库校验：手写 schema 校验（不引入 jsonschema 依赖，打包体积与离线可用优先）。

对应规格书第六节要求的检查项：
唯一 ID、重复规范表达、变体冲突、未知类别、数值范围、模式合法性、损坏数据不覆盖原库。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .models import (
    CATEGORY_KEYS,
    MATCH_TYPES,
    MODES,
    SCHEMA_VERSION,
    SOURCES,
    CombinationRule,
    LibrarySettings,
    PhraseEntry,
    PhraseLibrary,
    RuleCondition,
)


@dataclass
class ValidationReport:
    library: PhraseLibrary | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.library is not None and not self.errors

    def summary(self) -> str:
        if self.ok and self.library is not None:
            return f"词库校验通过：{len(self.library.entries)} 条词条"
        if self.errors:
            head = "；".join(self.errors[:3])
            more = f"（另有 {len(self.errors) - 3} 处问题）" if len(self.errors) > 3 else ""
            return f"词库校验失败：{head}{more}"
        return "词库校验失败：未知原因"


def _as_float(value: Any, default: float, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return max(low, min(high, number))


def _as_str_list(value: Any) -> list[str]:
    if isinstance(value, str):
        items = [part.strip() for part in value.replace("，", ",").split(",")]
    elif isinstance(value, (list, tuple)):
        items = [str(item).strip() for item in value]
    else:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def parse_entry(raw: Any, index: int, report: ValidationReport) -> PhraseEntry | None:
    """把 JSON 里的一个对象解析成 PhraseEntry；失败返回 None 并记录错误。"""
    if not isinstance(raw, dict):
        report.errors.append(f"第 {index} 条词条不是对象")
        return None

    entry_id = str(raw.get("id") or "").strip()
    canonical = str(raw.get("canonical_phrase") or raw.get("phrase") or "").strip()
    if not entry_id:
        report.errors.append(f"第 {index} 条词条缺少 id")
    if not canonical:
        report.errors.append(f"第 {index} 条词条（{entry_id or '?'}）缺少 canonical_phrase")

    category = str(raw.get("category") or "sacrifice").strip()
    if category not in CATEGORY_KEYS:
        report.errors.append(f"词条 {entry_id or index} 的类别未定义：{category}")
        category = "sacrifice"

    match_type = str(raw.get("match_type") or "phrase").strip()
    if match_type not in MATCH_TYPES:
        report.warnings.append(f"词条 {entry_id} 的 match_type 未知（{match_type}），按 phrase 处理")
        match_type = "phrase"

    modes = [mode for mode in _as_str_list(raw.get("modes")) if mode in MODES]
    if not modes:
        modes = ["serious", "chaos"]

    source = str(raw.get("source") or "user").strip()
    if source not in SOURCES:
        report.warnings.append(f"词条 {entry_id} 的 source 未知（{source}），按 user 处理")
        source = "user"

    weight = _as_float(raw.get("weight"), 0.8, 0.0, 2.0)
    min_confidence = _as_float(raw.get("min_confidence"), 0.0, 0.0, 1.0)
    comedic = _as_float(raw.get("comedic_potential"), 0.4, 0.0, 1.0)

    if not entry_id or not canonical:
        return None

    known = {
        "id",
        "canonical_phrase",
        "phrase",
        "variants",
        "category",
        "weight",
        "match_type",
        "modes",
        "context_tags",
        "negative_contexts",
        "min_confidence",
        "comedic_potential",
        "enabled",
        "source",
    }
    extra = {key: value for key, value in raw.items() if key not in known}

    variants = [text for text in _as_str_list(raw.get("variants")) if text != canonical]
    return PhraseEntry(
        id=entry_id,
        canonical_phrase=canonical,
        variants=variants,
        category=category,
        weight=weight,
        match_type=match_type,
        modes=modes,
        context_tags=_as_str_list(raw.get("context_tags")),
        negative_contexts=_as_str_list(raw.get("negative_contexts")),
        min_confidence=min_confidence,
        comedic_potential=comedic,
        enabled=bool(raw.get("enabled", True)),
        source=source,
        extra=extra,
    )


def _normalize_scope(value: Any, rule_id: str, report: ValidationReport) -> str:
    scope = str(value or "current").strip().lower()
    if scope not in ("current", "context", "any"):
        report.warnings.append(f"规则 {rule_id} 的 scope 未知（{scope}），按 current 处理")
        return "current"
    return scope


def parse_rule(raw: Any, index: int, report: ValidationReport) -> CombinationRule | None:
    if not isinstance(raw, dict):
        report.errors.append(f"第 {index} 条规则不是对象")
        return None

    rule_id = str(raw.get("id") or "").strip()
    name = str(raw.get("name") or rule_id).strip()
    if not rule_id:
        report.errors.append(f"第 {index} 条规则缺少 id")
        return None

    def conditions(key: str) -> list[RuleCondition]:
        items = raw.get(key) or []
        out: list[RuleCondition] = []
        if not isinstance(items, list):
            report.warnings.append(f"规则 {rule_id} 的 {key} 不是数组，已忽略")
            return out
        for item in items:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or "").strip()
            if not kind:
                report.warnings.append(f"规则 {rule_id} 的 {key} 有条件缺少 kind，已忽略")
                continue
            out.append(
                RuleCondition(
                    kind=kind,
                    value=str(item.get("value") or "").strip(),
                    threshold=_as_float(item.get("threshold"), 0.0, 0.0, 1.0),
                    scope=_normalize_scope(item.get("scope"), rule_id, report),
                )
            )
        return out

    modes = [mode for mode in _as_str_list(raw.get("modes")) if mode in MODES] or list(MODES)
    return CombinationRule(
        id=rule_id,
        name=name,
        requires=conditions("requires"),
        any_of=conditions("any_of"),
        excludes=conditions("excludes"),
        window_seconds=_as_float(raw.get("window_seconds"), 8.0, 0.0, 120.0),
        score_delta=_as_float(raw.get("score_delta"), 0.0, -1.0, 1.0),
        modes=modes,
        min_confidence=_as_float(raw.get("min_confidence"), 0.0, 0.0, 1.0),
        enabled=bool(raw.get("enabled", True)),
        extra={
            key: value
            for key, value in raw.items()
            if key
            not in {
                "id",
                "name",
                "requires",
                "any_of",
                "excludes",
                "window_seconds",
                "score_delta",
                "modes",
                "min_confidence",
                "enabled",
            }
        },
    )


def parse_library(raw: Any) -> ValidationReport:
    """把 JSON 字典解析成 PhraseLibrary，并把所有问题收集进报告。"""
    report = ValidationReport()
    if not isinstance(raw, dict):
        report.errors.append("词库根节点不是对象")
        return report

    schema_version = raw.get("schema_version", SCHEMA_VERSION)
    try:
        schema_version = int(schema_version)
    except (TypeError, ValueError):
        report.warnings.append(f"schema_version 非法（{schema_version!r}），按 1 处理")
        schema_version = SCHEMA_VERSION
    if schema_version > SCHEMA_VERSION:
        report.warnings.append(
            f"词库 schema_version={schema_version} 高于当前程序支持的 {SCHEMA_VERSION}，"
            "未知字段会被保留但不参与匹配"
        )

    settings_raw = raw.get("settings") or {}
    if not isinstance(settings_raw, dict):
        settings_raw = {}
    settings = LibrarySettings(
        deduplicate_variants=bool(settings_raw.get("deduplicate_variants", True)),
        normalize_chinese_punctuation=bool(settings_raw.get("normalize_chinese_punctuation", True)),
        default_mode=str(settings_raw.get("default_mode") or "serious"),
        fuzzy_enabled=bool(settings_raw.get("fuzzy_enabled", True)),
        fuzzy_min_similarity=_as_float(settings_raw.get("fuzzy_min_similarity"), 0.82, 0.5, 1.0),
    )

    entries_raw = raw.get("entries")
    if not isinstance(entries_raw, list):
        report.errors.append("缺少 entries 数组")
        return report

    entries: list[PhraseEntry] = []
    seen_ids: set[str] = set()
    seen_canonical: dict[str, str] = {}
    seen_forms: dict[str, str] = {}
    for index, item in enumerate(entries_raw):
        entry = parse_entry(item, index, report)
        if entry is None:
            continue
        if entry.id in seen_ids:
            report.errors.append(f"词条 id 重复：{entry.id}")
            continue
        seen_ids.add(entry.id)

        if settings.normalize_chinese_punctuation:
            canonical_key = _squash(entry.canonical_phrase)
        else:
            canonical_key = entry.canonical_phrase
        if canonical_key in seen_canonical:
            report.warnings.append(
                f"规范表达重复：{entry.canonical_phrase}（与 {seen_canonical[canonical_key]} 相同）"
            )
        else:
            seen_canonical[canonical_key] = entry.id

        for form in entry.all_forms():
            key = _squash(form) if settings.normalize_chinese_punctuation else form
            owner = seen_forms.get(key)
            if owner is not None and owner != entry.id:
                report.warnings.append(f"表达冲突：{form} 同时出现在 {owner} 与 {entry.id}")
            else:
                seen_forms[key] = entry.id

        entries.append(entry)

    rules_raw = raw.get("rules") or []
    rules: list[CombinationRule] = []
    seen_rule_ids: set[str] = set()
    if isinstance(rules_raw, list):
        for index, item in enumerate(rules_raw):
            rule = parse_rule(item, index, report)
            if rule is None:
                continue
            if rule.id in seen_rule_ids:
                report.errors.append(f"规则 id 重复：{rule.id}")
                continue
            seen_rule_ids.add(rule.id)
            rules.append(rule)

    report.library = PhraseLibrary(
        schema_version=SCHEMA_VERSION,
        library_name=str(raw.get("library_name") or "Rusty Moment Library"),
        language=_as_str_list(raw.get("language")) or ["zh-CN"],
        entries=entries,
        rules=rules,
        settings=settings,
    )
    return report


def _squash(text: str) -> str:
    """去标点与空白，用于重复检测（不影响实际匹配）。"""
    drop = "，。！？、,.!?;；:：\"'“”‘’（）()【】[]…—～~ "
    return "".join(char for char in text if char not in drop).lower()


def library_to_dict(library: PhraseLibrary) -> dict[str, Any]:
    """序列化回 JSON（保留 extra 里的未知字段，便于向前兼容）。"""

    def entry_dict(entry: PhraseEntry) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": entry.id,
            "canonical_phrase": entry.canonical_phrase,
            "variants": list(entry.variants),
            "category": entry.category,
            "weight": round(entry.weight, 3),
            "match_type": entry.match_type,
            "modes": list(entry.modes),
            "context_tags": list(entry.context_tags),
            "negative_contexts": list(entry.negative_contexts),
            "min_confidence": round(entry.min_confidence, 3),
            "comedic_potential": round(entry.comedic_potential, 3),
            "enabled": bool(entry.enabled),
            "source": entry.source,
        }
        data.update(entry.extra)
        return data

    def rule_dict(rule: CombinationRule) -> dict[str, Any]:
        def cond(item: RuleCondition) -> dict[str, Any]:
            out: dict[str, Any] = {"kind": item.kind}
            if item.value:
                out["value"] = item.value
            if item.threshold:
                out["threshold"] = round(item.threshold, 3)
            if item.scope and item.scope != "current":
                out["scope"] = item.scope
            return out

        data: dict[str, Any] = {
            "id": rule.id,
            "name": rule.name,
            "requires": [cond(item) for item in rule.requires],
            "any_of": [cond(item) for item in rule.any_of],
            "excludes": [cond(item) for item in rule.excludes],
            "window_seconds": rule.window_seconds,
            "score_delta": round(rule.score_delta, 3),
            "modes": list(rule.modes),
            "min_confidence": round(rule.min_confidence, 3),
            "enabled": bool(rule.enabled),
        }
        data.update(rule.extra)
        return data

    return {
        "schema_version": library.schema_version,
        "library_name": library.library_name,
        "language": list(library.language),
        "entries": [entry_dict(entry) for entry in library.entries],
        "rules": [rule_dict(rule) for rule in library.rules],
        "settings": {
            "deduplicate_variants": library.settings.deduplicate_variants,
            "normalize_chinese_punctuation": library.settings.normalize_chinese_punctuation,
            "default_mode": library.settings.default_mode,
            "fuzzy_enabled": library.settings.fuzzy_enabled,
            "fuzzy_min_similarity": library.settings.fuzzy_min_similarity,
        },
    }

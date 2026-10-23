"""词库存储：加载、原子保存（带备份）、内置词库、旧配置迁移与合并。

设计要点：
- 加载失败不会破坏磁盘上的原文件（只报错并回退到内置词库）；
- 保存走「临时文件 + 覆盖 + 备份」，避免写一半断电留下坏文件；
- 旧版 ``keywords.positives/exclusions`` 可以无损转换成结构化词条（source=legacy），
  这样现有用户升级后不会丢自建词库。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any, Iterable

from .models import PhraseEntry, PhraseLibrary
from .schema import ValidationReport, library_to_dict, parse_library

log = logging.getLogger(__name__)

BUILTIN_LIBRARY_NAME = "core_zh.json"

# 旧版词条没有类别信息，按短语里的线索猜一个（猜错只影响权重与适用模式，不影响匹配）
_LEGACY_CATEGORY_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("sacrifice", ("拖住", "断后", "掩护", "撑住", "顶住", "拦住", "争取时间")),
    ("farewell", ("报仇", "照顾好", "走了", "下次", "记住", "托付", "回不去")),
    ("defeat", ("尽力", "不行", "没了", "输了", "算了", "撑不住", "认输")),
    ("teammate", ("你们", "兄弟", "队友", "快走", "先走")),
    ("jargon", ("没子弹", "没甲", "没药", "倒了", "寄了", "卡了")),
)


# --------------------------------------------------------------------------- #
# 路径
# --------------------------------------------------------------------------- #


def builtin_library_path() -> Path:
    """内置词库路径（打包后位于 _MEIPASS 内的同名目录）。"""
    return Path(__file__).resolve().parent / "data" / BUILTIN_LIBRARY_NAME


def builtin_rules_path() -> Path:
    return Path(__file__).resolve().parent / "data" / "rules.json"


# --------------------------------------------------------------------------- #
# 加载 / 保存
# --------------------------------------------------------------------------- #


def load_library(path: str | Path | None = None) -> ValidationReport:
    """从 JSON 加载词库；路径为空时读内置词库。失败返回带错误的报告。"""
    target = Path(path) if path is not None else builtin_library_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        report = ValidationReport()
        report.errors.append(f"词库文件不存在：{target}")
        return report
    except json.JSONDecodeError as exc:
        report = ValidationReport()
        report.errors.append(f"词库 JSON 解析失败（{target.name}）：{exc}")
        return report
    except OSError as exc:
        report = ValidationReport()
        report.errors.append(f"词库读取失败（{target.name}）：{exc}")
        return report

    report = parse_library(raw)
    if report.library is not None:
        report.library.library_name = report.library.library_name or target.stem
    return report


def load_builtin() -> PhraseLibrary:
    """加载内置词库；失败时返回空库并记日志（程序必须还能跑）。"""
    report = load_library()
    if report.library is None:
        log.error("内置词库加载失败：%s", report.summary())
        return PhraseLibrary(library_name="Rusty Moment（内置词库缺失）")
    if report.errors:
        log.warning("内置词库存在问题：%s", "；".join(report.errors[:3]))
    if report.warnings:
        log.debug("内置词库警告：%s", "；".join(report.warnings[:5]))
    return report.library


def save_library(library: PhraseLibrary, path: str | Path) -> Path:
    """原子保存：先写临时文件，再替换，并保留一份 .bak 备份。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(library_to_dict(library), ensure_ascii=False, indent=2) + "\n"
    temp = target.with_suffix(target.suffix + ".tmp")
    temp.write_text(payload, encoding="utf-8")

    if target.exists():
        backup = target.with_suffix(target.suffix + ".bak")
        try:
            shutil.copy2(target, backup)
        except OSError as exc:  # 备份失败不阻断主流程
            log.warning("词库备份失败（%s）：%s", backup.name, exc)

    os.replace(temp, target)
    return target


def load_or_default(path: str | Path | None = None) -> tuple[PhraseLibrary, list[str], list[str]]:
    """给运行时用的入口：返回 (词库, 错误列表, 警告列表)，永不抛异常。"""
    report = load_library(path)
    if report.library is None:
        fallback = load_builtin()
        return fallback, report.errors, report.warnings
    return report.library, report.errors, report.warnings


# --------------------------------------------------------------------------- #
# 合并
# --------------------------------------------------------------------------- #


def merge_libraries(
    base: PhraseLibrary, overlay: PhraseLibrary, *, overwrite: bool = True
) -> PhraseLibrary:
    """把 overlay 合并进 base。

    ``overwrite=True``（默认）：同 id 覆盖，同规范表达以 overlay 为准——用于「用户库覆盖内置」。
    ``overwrite=False``：同规范表达保留 base 的版本——用于把旧关键词并入内置词库时
    不污染内置的类别与权重。

    规则用 id 覆盖；settings 以 base 为准（内置词库的匹配策略不该被用户库改掉）。
    """
    merged = base.clone()
    by_canonical = {_squash(entry.canonical_phrase): entry.id for entry in merged.entries}

    for entry in overlay.entries:
        key = _squash(entry.canonical_phrase)
        existing_id = by_canonical.get(key)
        if existing_id is not None and existing_id != entry.id:
            if not overwrite:
                continue
            # 规范表达撞车：保留用户在 overlay 里的写法
            merged.remove_entry(existing_id)
        merged.add_entries([entry])
        by_canonical[key] = entry.id

    rules_by_id = {rule.id: index for index, rule in enumerate(merged.rules)}
    for rule in overlay.rules:
        index = rules_by_id.get(rule.id)
        if index is None:
            merged.rules.append(rule)
            rules_by_id[rule.id] = len(merged.rules) - 1
        else:
            merged.rules[index] = rule
    return merged


def _squash(text: str) -> str:
    drop = "，。！？、,.!?;；:：\"'“”‘’（）()【】[]…—～~ "
    return "".join(char for char in text if char not in drop).lower()


# --------------------------------------------------------------------------- #
# 旧配置迁移
# --------------------------------------------------------------------------- #


def legacy_category(phrase: str, negative: bool = False) -> str:
    if negative:
        return "exclusion"
    for category, hints in _LEGACY_CATEGORY_HINTS:
        if any(hint in phrase for hint in hints):
            return category
    return "jargon"


def library_from_legacy_keywords(
    positives: Iterable[dict[str, Any]],
    exclusions: Iterable[dict[str, Any]],
    *,
    library_name: str = "旧版关键词迁移",
) -> PhraseLibrary:
    """把旧版 ``keywords.positives/exclusions`` 转成结构化词库。"""
    library = PhraseLibrary(library_name=library_name)
    counter = {"legacy_pos": 0, "legacy_neg": 0}

    def build(items: Iterable[dict[str, Any]], negative: bool) -> None:
        prefix = "legacy_neg" if negative else "legacy_pos"
        for item in items or []:
            if isinstance(item, str):
                phrase, weight, aliases = item, 1.0, []
            elif isinstance(item, dict):
                phrase = str(item.get("phrase") or "").strip()
                try:
                    weight = float(item.get("weight", 1.0))
                except (TypeError, ValueError):
                    weight = 1.0
                raw_aliases = item.get("aliases") or []
                aliases = [str(alias).strip() for alias in raw_aliases if str(alias).strip()]
            else:
                continue
            if not phrase:
                continue
            counter[prefix] += 1
            library.entries.append(
                PhraseEntry(
                    id=f"{prefix}_{counter[prefix]:03d}",
                    canonical_phrase=phrase,
                    variants=aliases,
                    category=legacy_category(phrase, negative=negative),
                    weight=max(0.0, min(2.0, weight)),
                    match_type="phrase",
                    modes=["serious", "chaos"],
                    source="legacy",
                    extra={"migrated_from": "keywords"},
                )
            )

    build(positives, negative=False)
    build(exclusions, negative=True)
    return library


# --------------------------------------------------------------------------- #
# 导入 / 导出
# --------------------------------------------------------------------------- #


def export_csv(library: PhraseLibrary, path: str | Path) -> Path:
    """导出 CSV（Excel 可编辑）；复杂组合规则仍以 JSON 为准。"""
    import csv

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "id",
                "canonical_phrase",
                "variants",
                "category",
                "weight",
                "match_type",
                "modes",
                "context_tags",
                "min_confidence",
                "comedic_potential",
                "enabled",
                "source",
            ]
        )
        for entry in library.entries:
            writer.writerow(
                [
                    entry.id,
                    entry.canonical_phrase,
                    " | ".join(entry.variants),
                    entry.category,
                    f"{entry.weight:g}",
                    entry.match_type,
                    "/".join(entry.modes),
                    "/".join(entry.context_tags),
                    f"{entry.min_confidence:g}",
                    f"{entry.comedic_potential:g}",
                    "1" if entry.enabled else "0",
                    entry.source,
                ]
            )
    return target


def import_library(path: str | Path) -> ValidationReport:
    """导入外部 JSON 词库（只校验，不落盘；由调用方决定合并策略）。"""
    return load_library(path)


def backup_path(path: str | Path) -> Path:
    """生成带时间戳的备份文件名（供「恢复默认」前存档用）。"""
    target = Path(path)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return target.with_name(f"{target.stem}.{stamp}.bak.json")

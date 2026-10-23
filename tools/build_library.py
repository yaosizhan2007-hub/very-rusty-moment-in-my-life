"""内置词库构建工具：把 data/parts/*.json 合并成 data/core_zh.json 并校验。

用法（项目根目录）：
    .venv\\Scripts\\python.exe tools/build_library.py
    .venv\\Scripts\\python.exe tools/build_library.py --check   # 只校验不写文件

为什么分文件维护：
- 每个类别一个文件，方便单独扩充、审阅与调整权重；
- 运行时的主数据源仍然是合并后的 core_zh.json（用户可整份导入/导出）；
- 合并时会做 id 唯一、规范表达重复、表达冲突、类别合法性校验，
  有任何错误直接失败，不会写出半个词库。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rusty_moment.phrases.schema import library_to_dict, parse_library  # noqa: E402

PARTS_DIR = ROOT / "rusty_moment" / "phrases" / "data" / "parts"
OUTPUT = ROOT / "rusty_moment" / "phrases" / "data" / "core_zh.json"
RULES = ROOT / "rusty_moment" / "phrases" / "data" / "rules.json"


def load_parts() -> dict:
    if not PARTS_DIR.exists():
        raise SystemExit(f"找不到词库片段目录：{PARTS_DIR}")

    entries: list[dict] = []
    rules: list[dict] = []
    settings: dict = {}
    language = ["zh-CN"]
    for path in sorted(PARTS_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        part_entries = data.get("entries") or []
        entries.extend(part_entries)
        rules.extend(data.get("rules") or [])
        settings.update(data.get("settings") or {})
        if isinstance(data.get("language"), list):
            language = data["language"]
        print(f"  {path.name:28s} {len(part_entries):3d} 条")

    if RULES.exists():
        extra = json.loads(RULES.read_text(encoding="utf-8"))
        extra_rules = extra.get("rules") or []
        rules.extend(extra_rules)
        print(f"  {'rules.json':28s} {len(extra_rules):3d} 条规则")

    return {
        "schema_version": 1,
        "library_name": "Rusty Moment Core Library",
        "language": language,
        "entries": entries,
        "rules": rules,
        "settings": {
            "deduplicate_variants": True,
            "normalize_chinese_punctuation": True,
            "default_mode": "serious",
            "fuzzy_enabled": True,
            "fuzzy_min_similarity": 0.82,
            **settings,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="只校验，不写文件")
    args = parser.parse_args()

    print("读取片段：")
    raw = load_parts()
    print(f"\n合计：{len(raw['entries'])} 条词条、{len(raw['rules'])} 条规则")

    report = parse_library(raw)
    print("\n校验结果：", report.summary())
    if report.errors:
        print("\n错误：")
        for item in report.errors:
            print("  ✗", item)
    if report.warnings:
        print("\n警告：")
        for item in report.warnings[:20]:
            print("  ⚠", item)
        if len(report.warnings) > 20:
            print(f"  …（共 {len(report.warnings)} 条警告）")

    if report.library is None or report.errors:
        return 1

    counts = report.library.category_counts()
    print("\n分类统计：")
    for key, value in counts.items():
        if value:
            print(f"  {key:12s} {value:3d}")

    if args.check:
        print("\n（--check：未写文件）")
        return 0

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        json.dumps(library_to_dict(report.library), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    size_kb = OUTPUT.stat().st_size / 1024
    print(f"\n已写入 {OUTPUT.relative_to(ROOT)}（{size_kb:.1f} KB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

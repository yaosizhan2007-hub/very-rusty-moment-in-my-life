"""打包后瘦身（可选）：删除本程序用不到的 Qt 组件，减小发行版体积。

用法（打包完成后执行）：
    .venv\\Scripts\\python.exe packaging\\slim_after_build.py
    .venv\\Scripts\\python.exe packaging\\slim_after_build.py --dry-run

注意：删完必须重新启动一次 exe 验证界面能起来。脚本只会删除固定名单里的文件，
不会动 QtCore/QtGui/QtWidgets 与平台插件。
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

# 相对 dist/RustyMoment/_internal/PySide6 的路径
UNUSED_FILES = [
    "opengl32sw.dll",  # 软件 OpenGL 回退，约占 20MB；只有无 GPU 的机器才需要
    "Qt6Quick.dll",
    "Qt6Qml.dll",
    "Qt6Pdf.dll",
    "Qt6QuickControls2.dll",
    "Qt6QmlModels.dll",
    "Qt6QmlWorkerScript.dll",
    "Qt6VirtualKeyboard.dll",
]

UNUSED_DIRS = [
    "qml",
    "translations",
]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Rusty Moment 打包瘦身")
    parser.add_argument(
        "--dist",
        default=str(Path(__file__).resolve().parents[1] / "dist" / "RustyMoment"),
        help="onedir 产物目录",
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印，不删除")
    args = parser.parse_args(argv)

    base = Path(args.dist)
    pyside = base / "_internal" / "PySide6"
    if not pyside.exists():
        print(f"找不到 {pyside}，请先运行 PyInstaller 打包")
        return 1

    removed = 0
    for name in UNUSED_FILES:
        target = pyside / name
        if target.exists():
            size = target.stat().st_size
            print(f"{'[dry] ' if args.dry_run else ''}删除文件 {name} ({size / 1e6:.1f}MB)")
            removed += size
            if not args.dry_run:
                target.unlink()
    for name in UNUSED_DIRS:
        target = pyside / name
        if target.exists():
            size = sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
            print(f"{'[dry] ' if args.dry_run else ''}删除目录 {name}/ ({size / 1e6:.1f}MB)")
            removed += size
            if not args.dry_run:
                shutil.rmtree(target, ignore_errors=True)

    total = sum(f.stat().st_size for f in base.rglob("*") if f.is_file())
    print(f"预计/实际释放 {removed / 1e6:.1f}MB，当前总大小 {total / 1e6:.1f}MB")
    print("务必重新启动一次 exe 确认界面正常。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

"""Rusty Moment 入口。

用法：
    python main.py            启动图形界面
    python main.py --debug    以 DEBUG 级别启动日志
    python main.py --check    只做环境自检（不启动界面，排查问题用）
    python main.py --perf     跑一遍性能测量（结果写日志目录）
"""

from __future__ import annotations

import sys


def _configure_stdio() -> None:
    """Windows 控制台默认 GBK，打印 ✅ 之类的字符会直接抛 UnicodeEncodeError。

    这里只放宽错误处理（不强制改编码，免得中文变乱码），确保自检输出
    永远能打出来，而不是在最后一步崩掉。
    """
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - 老版本或已重定向的流，忽略即可
            pass


def main() -> int:
    _configure_stdio()
    argv = [arg for arg in sys.argv if not arg.startswith("--")]
    debug = "--debug" in sys.argv
    if debug:
        import logging

        logging.getLogger().setLevel(logging.DEBUG)

    if "--check" in sys.argv or "--self-test" in sys.argv:
        from rusty_moment.diagnostics import run_checks

        return run_checks(check_tracks="--no-tracks" not in sys.argv)

    if "--perf" in sys.argv:
        from rusty_moment.perf_probe import run_perf

        return run_perf(
            include_mic="--no-mic" not in sys.argv,
            include_stt="--no-stt" not in sys.argv,
            include_ai="--no-ai" not in sys.argv,
            include_obs="--no-obs" not in sys.argv,
        )

    from rusty_moment.app import run

    return run(argv)


if __name__ == "__main__":
    raise SystemExit(main())

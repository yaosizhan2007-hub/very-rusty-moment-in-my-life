"""Rusty Moment 入口。

用法：
    python main.py            启动图形界面
    python main.py --debug    以 DEBUG 级别启动日志
    python main.py --check    只做环境自检（不启动界面，排查问题用）
    python main.py --perf     跑一遍性能测量（结果写日志目录）
"""

from __future__ import annotations

import sys


def main() -> int:
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

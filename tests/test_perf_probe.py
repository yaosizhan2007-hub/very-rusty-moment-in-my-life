"""性能探针测试：只验证代码路径与输出，不追求具体数值。"""

from __future__ import annotations

import sys

import pytest

from rusty_moment.perf_probe import (
    PerfRow,
    ProcessStats,
    measure_idle,
    measure_vad_offline,
    run_perf,
)


def test_perf_row_formatting():
    row = PerfRow(item="x", detail="d", metrics={"CPU": "1.0%", "内存": "2MB"})
    assert row.format_metrics() == "CPU=1.0%  内存=2MB"


def test_process_stats_available_on_windows():
    stats = ProcessStats()
    if sys.platform == "win32":
        assert stats.available
        assert stats.memory_mb() > 0
    else:  # pragma: no cover
        assert not stats.available


@pytest.mark.skipif(sys.platform != "win32", reason="进程采样仅 Windows")
def test_measure_idle_returns_metrics():
    row = measure_idle(ProcessStats(), seconds=0.3)
    assert row.item == "空闲"
    assert "CPU" in row.metrics and "内存" in row.metrics


def test_measure_vad_offline_is_fast():
    row = measure_vad_offline()
    assert row.item == "VAD 切句"
    assert "单次" in row.metrics


def test_run_perf_writes_report(tmp_path, monkeypatch):
    import rusty_moment.utils.logging_setup as logging_setup

    monkeypatch.setattr(logging_setup, "default_log_dir", lambda: tmp_path / "logs")
    code = run_perf(
        seconds=0.2,
        include_mic=False,
        include_stt=False,
        include_ai=False,
        include_obs=False,
    )
    assert code == 0
    report = tmp_path / "logs" / "perf-report.md"
    assert report.exists()
    text = report.read_text(encoding="utf-8")
    assert "Rusty Moment 性能实测" in text
    assert "VAD 切句" in text

"""自检模块测试：不依赖真实声卡之外的环境，只验证输出与退出码。"""

from __future__ import annotations

import numpy as np
import soundfile as sf

from rusty_moment.config.config_manager import ConfigManager
from rusty_moment.diagnostics import (
    check_audio_backends,
    check_library,
    check_python,
    run_checks,
)


def test_check_python_reports_version():
    lines = check_python()
    assert any("Python" in line for line in lines)
    assert any("打包运行" in line for line in lines)


def test_audio_backends_are_reported():
    lines = check_audio_backends()
    text = "\n".join(lines)
    assert "sounddevice" in text
    assert "libsndfile" in text
    assert "PyAV" in text


def test_run_checks_writes_file_and_passes(tmp_path, monkeypatch):
    import rusty_moment.utils.logging_setup as logging_setup

    monkeypatch.setattr(logging_setup, "default_log_dir", lambda: tmp_path / "logs")
    code = run_checks(check_tracks=False)
    assert code == 0
    written = tmp_path / "logs" / "self-check.txt"
    assert written.exists()
    assert "结论" in written.read_text(encoding="utf-8")


def test_library_check_reports_missing_file(tmp_path):
    manager = ConfigManager(config_path=tmp_path / "config.json")
    cfg = manager.load()
    cfg.audio.playlist = [{"path": str(tmp_path / "gone.aac"), "title": "消失的歌"}]
    cfg.audio.current_index = 0
    cfg.audio.discovered = True
    manager.save(cfg)

    lines = check_library(tmp_path / "config.json")
    assert any("文件不存在" in line for line in lines)


def test_library_check_decodes_real_file(tmp_path):
    audio_path = tmp_path / "tone.wav"
    sf.write(str(audio_path), np.zeros((800, 2), dtype=np.float32), 8000)

    manager = ConfigManager(config_path=tmp_path / "config.json")
    cfg = manager.load()
    cfg.audio.playlist = [{"path": str(audio_path), "title": "测试音"}]
    cfg.audio.current_index = 0
    cfg.audio.discovered = True
    manager.save(cfg)

    lines = check_library(tmp_path / "config.json")
    assert any("后端 soundfile" in line for line in lines)


def test_library_check_without_decode(tmp_path):
    audio_path = tmp_path / "tone.wav"
    sf.write(str(audio_path), np.zeros((800, 2), dtype=np.float32), 8000)
    manager = ConfigManager(config_path=tmp_path / "config.json")
    cfg = manager.load()
    cfg.audio.playlist = [{"path": str(audio_path), "title": "测试音"}]
    cfg.audio.discovered = True
    manager.save(cfg)

    lines = check_library(tmp_path / "config.json", decode=False)
    assert any("测试音" in line for line in lines)
    assert not any("后端" in line for line in lines)

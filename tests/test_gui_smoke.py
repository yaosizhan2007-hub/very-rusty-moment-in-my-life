"""GUI 冒烟测试（offscreen）：主窗口能构建、核心按钮能跑通事件流程。

用替身播放器与替身 OBS 传输层，不依赖真实声卡与 OBS。
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("pytestqt")
pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from fakes import FakeOBS, FakePlayer  # noqa: E402
from rusty_moment.app import build_context  # noqa: E402
from rusty_moment.config.config_manager import AppConfig, ConfigManager  # noqa: E402
from rusty_moment.obs.obs_client import OBSClient  # noqa: E402
from rusty_moment.security.secret_store import SecretStore  # noqa: E402
from rusty_moment.ui.qt_bridge import EventBridge  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def make_window(tmp_path, qapp):
    from rusty_moment.ui.main_window import MainWindow

    obs = FakeOBS()
    config = AppConfig()
    config.audio.playlist = []
    config.obs.game_source = "桌面音频"
    config.trigger.cooldown_seconds = 0.0
    manager = ConfigManager(config_path=tmp_path / "config.json")
    bridge = EventBridge()
    client = OBSClient(
        password_provider=lambda: obs.password,
        transport_factory=obs.transport_factory(),
        timeout=1.0,
    )
    ctx = build_context(
        config,
        manager,
        secret_store=SecretStore(tmp_path / "secrets.bin"),
        bridge=bridge,
        player=FakePlayer(duration=0.1),
        obs_client=client,
    )
    window = MainWindow(ctx)
    return window, ctx, obs


def test_main_window_builds(tmp_path, qapp, qtbot):
    window, ctx, _obs = make_window(tmp_path, qapp)
    qtbot.addWidget(window)
    window.show()
    assert window.force_button.isEnabled()
    assert window.auto_check.isChecked()
    assert "智能悲壮" in window.mode_label.text()
    window.close()


def test_force_trigger_button_runs_event(tmp_path, qapp, qtbot):
    window, ctx, _obs = make_window(tmp_path, qapp)
    qtbot.addWidget(window)
    window.show()

    qtbot.mouseClick(window.force_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: window.log_widget.records_count() >= 1, timeout=6000)
    assert window.log_widget.records_count() >= 1
    window.close()


def test_toggle_auto_writes_config(tmp_path, qapp, qtbot):
    window, ctx, _obs = make_window(tmp_path, qapp)
    qtbot.addWidget(window)
    window.show()
    window.auto_check.setChecked(False)
    assert ctx.config.mode.auto_trigger_enabled is False
    assert manager_saved(tmp_path)
    window.close()


def manager_saved(tmp_path) -> bool:
    path = tmp_path / "config.json"
    return path.exists() and "auto_trigger_enabled" in path.read_text(encoding="utf-8")


def test_mode_switch_to_chaotic(tmp_path, qapp, qtbot):
    window, ctx, _obs = make_window(tmp_path, qapp)
    qtbot.addWidget(window)
    window.show()
    index = window.mode_combo.findData("chaotic")
    window.mode_combo.setCurrentIndex(index)
    assert ctx.config.mode.current == "chaotic"
    assert "抽象整活" in window.mode_label.text()
    window.close()


def test_phrase_text_roundtrip_with_aliases(qapp):
    """设置页的词库文本支持「词组|权重|近义说法」，往返不丢信息。"""
    from rusty_moment.ui.settings_dialog import _format_phrases, _parse_phrases

    items = [
        {"phrase": "别管我", "weight": 1.0, "aliases": ["别救我", "不要救我"]},
        {"phrase": "快走", "weight": 0.5},
    ]
    text = _format_phrases(items)
    assert "别管我|1|别救我,不要救我" in text

    parsed = _parse_phrases(text)
    assert parsed[0]["phrase"] == "别管我"
    assert parsed[0]["aliases"] == ["别救我", "不要救我"]
    assert "aliases" not in parsed[1]


def test_phrase_text_accepts_chinese_comma_aliases(qapp):
    from rusty_moment.ui.settings_dialog import _parse_phrases

    parsed = _parse_phrases("别管我|1|别救我，不要管我、别理我")
    assert parsed[0]["aliases"] == ["别救我", "不要管我", "别理我"]


def test_output_mode_switch_to_local(tmp_path, qapp, qtbot):
    """切到「仅本地播歌」：写入配置、禁用连接按钮、状态灯改文案。"""
    window, ctx, _obs = make_window(tmp_path, qapp)
    qtbot.addWidget(window)
    window.show()

    index = window.output_combo.findData(False)
    window.output_combo.setCurrentIndex(index)

    assert ctx.config.obs.control_enabled is False
    assert not window.connect_button.isEnabled(), "纯本地模式不需要连接 OBS"
    assert "仅本地" in window.obs_label.text()
    assert "只监听麦克风" in window.output_hint.text() or "不开 OBS" in window.output_hint.text()

    back = window.output_combo.findData(True)
    window.output_combo.setCurrentIndex(back)
    assert ctx.config.obs.control_enabled is True
    assert window.connect_button.isEnabled()
    window.close()


def test_track_switch_updates_config(tmp_path, qapp, qtbot):
    window, ctx, _obs = make_window(tmp_path, qapp)
    qtbot.addWidget(window)
    # 直接给配置塞两首（不依赖真实文件）
    ctx.config.audio.playlist = [
        {"path": "C:/music/a.mp3", "title": "第一首"},
        {"path": "C:/music/b.mp3", "title": "第二首"},
    ]
    ctx.config.audio.current_index = 0
    window._refresh_track_combo()
    assert window.track_combo.count() == 2
    assert "第一首" in window.track_combo.itemText(0)

    window.track_combo.setCurrentIndex(1)
    assert ctx.config.audio.current_index == 1
    assert ctx.config.audio.bgm_title == "第二首"
    assert "文件缺失" in window.track_combo.itemText(1), "不存在的曲目要标出来"
    window.close()


def test_settings_playlist_can_be_edited(tmp_path, qapp, qtbot):
    from rusty_moment.ui.settings_dialog import SettingsDialog

    window, ctx, _obs = make_window(tmp_path, qapp)
    qtbot.addWidget(window)
    ctx.config.audio.playlist = [{"path": "C:/music/a.mp3", "title": "第一首"}]
    dialog = SettingsDialog(
        ctx.config,
        obs_controller=ctx.obs_controller,
        audio_player=ctx.player,
        secret_store=ctx.secret_store,
        mic_capture=ctx.capture,
        parent=window,
    )
    qtbot.addWidget(dialog)
    assert dialog.track_list.count() == 1

    dialog._tracks.append({"path": "C:/music/b.mp3", "title": "第二首"})
    dialog._refresh_track_list()
    dialog._move_track(-1)
    cfg = dialog.result_config()
    assert len(cfg.audio.playlist) == 2
    assert cfg.audio.playlist[0]["title"] in ("第一首", "第二首")
    window.close()


def test_settings_dialog_opens_and_saves(tmp_path, qapp, qtbot):
    from rusty_moment.ui.settings_dialog import SettingsDialog

    window, ctx, _obs = make_window(tmp_path, qapp)
    qtbot.addWidget(window)
    dialog = SettingsDialog(
        ctx.config,
        obs_controller=ctx.obs_controller,
        audio_player=ctx.player,
        secret_store=ctx.secret_store,
        parent=window,
    )
    qtbot.addWidget(dialog)
    dialog.volume_spin.setValue(55)
    cfg = dialog.result_config()
    assert cfg.audio.volume == pytest.approx(0.55)
    window.close()

"""配置管理器测试：默认生成、损坏恢复、类型纠偏、权重归一化、快捷键冲突。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rusty_moment.config.config_manager import (
    AppConfig,
    ConfigCorruptError,
    ConfigManager,
    CURRENT_CONFIG_VERSION,
)

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "config" / "default_config.json"


def make_manager(tmp_path: Path, template: Path | None = None) -> ConfigManager:
    return ConfigManager(config_path=tmp_path / "config.json", template_path=template or TEMPLATE)


def test_creates_default_config_when_missing(tmp_path):
    manager = make_manager(tmp_path)
    config = manager.load()
    assert isinstance(config, AppConfig)
    assert manager.path.exists()
    assert config.trigger.cooldown_seconds == pytest.approx(30.0)


def test_roundtrip_save_and_load(tmp_path):
    manager = make_manager(tmp_path)
    config = manager.load()
    config.obs.game_source = "游戏音频"
    config.audio.volume = 0.42
    config.mode.current = "chaotic"
    manager.save(config)

    reloaded = make_manager(tmp_path).load()
    assert reloaded.obs.game_source == "游戏音频"
    assert reloaded.audio.volume == pytest.approx(0.42)
    assert reloaded.mode.current == "chaotic"


def test_corrupt_json_is_backed_up_and_reported(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{ this is not json", encoding="utf-8")
    manager = ConfigManager(config_path=path, template_path=TEMPLATE)

    with pytest.raises(ConfigCorruptError) as excinfo:
        manager.load()
    assert excinfo.value.backup_path is not None
    assert Path(excinfo.value.backup_path).exists()
    assert not path.exists()

    # 恢复默认后程序可以继续运行
    config = manager.reset_to_default()
    assert config.trigger.cooldown_seconds > 0
    assert manager.path.exists()


def test_future_version_is_rejected(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"version": 99}), encoding="utf-8")
    manager = ConfigManager(config_path=path, template_path=TEMPLATE)
    with pytest.raises(ConfigCorruptError):
        manager.load()


def test_invalid_values_are_coerced(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "audio": {"volume": 4.2, "bgm_path": 123},
                "obs": {"port": "not-a-port", "control_strategy": "explode"},
                "trigger": {"cooldown_seconds": -5, "trigger_threshold": 2},
                "mode": {"current": "nonsense"},
            }
        ),
        encoding="utf-8",
    )
    manager = ConfigManager(config_path=path, template_path=TEMPLATE)
    config, issues = manager.load_with_issues()

    assert config.audio.volume == 1.0
    assert config.audio.bgm_path == "123"
    assert config.obs.port == 4455
    assert config.obs.control_strategy == "mute"
    assert config.trigger.cooldown_seconds == 0.0
    assert config.trigger.trigger_threshold == 1.0
    assert config.mode.current == "serious"


def test_weights_are_normalized(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"trigger": {"weights": {"ai": 3, "keyword": 1, "rule": 0}}}),
        encoding="utf-8",
    )
    config, _ = ConfigManager(config_path=path, template_path=TEMPLATE).load_with_issues()
    weights = config.trigger.weights
    assert sum(weights.values()) == pytest.approx(1.0)
    assert weights["ai"] == pytest.approx(0.75)


def test_probabilities_are_bounded(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"trigger": {"probabilities": {"serious": 5, "chaotic": -1}}}),
        encoding="utf-8",
    )
    config, _ = ConfigManager(config_path=path, template_path=TEMPLATE).load_with_issues()
    assert config.trigger.probabilities["serious"] == 1.0
    assert config.trigger.probabilities["chaotic"] == 0.0


def test_hotkey_conflict_disables_duplicate(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"hotkeys": {"force_trigger": "F8", "stop_music": "F8", "toggle_auto": "F10"}}),
        encoding="utf-8",
    )
    config, issues = ConfigManager(config_path=path, template_path=TEMPLATE).load_with_issues()
    assert config.hotkeys.force_trigger == "F8"
    assert config.hotkeys.stop_music == ""
    assert any("快捷键冲突" in item for item in issues)


def test_enabled_ai_without_backend_is_disabled(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"ai": {"enabled": True, "backend": "none"}}), encoding="utf-8")
    config, issues = ConfigManager(config_path=path, template_path=TEMPLATE).load_with_issues()
    assert config.ai.enabled is False
    assert any("AI" in item for item in issues)


def test_missing_template_falls_back_to_builtin_defaults(tmp_path):
    manager = ConfigManager(config_path=tmp_path / "config.json", template_path=tmp_path / "nope.json")
    config = manager.load()
    assert config.trigger.cooldown_seconds == pytest.approx(30.0)
    assert config.keywords.positives, "内置默认关键词库不应为空"


def test_template_file_is_valid():
    raw = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    config, issues = AppConfig.from_dict(raw)
    assert config.version == CURRENT_CONFIG_VERSION
    assert not [item for item in issues if "非法" in item]


def test_saved_config_contains_no_secrets(tmp_path):
    manager = make_manager(tmp_path)
    manager.save(manager.load())
    text = manager.path.read_text(encoding="utf-8")
    assert "password" not in text.lower()
    assert "api_key" not in text.lower()


# --------------------------------------------------------------------------- #
# 播放列表
# --------------------------------------------------------------------------- #
def test_legacy_single_track_is_migrated(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"version": 1, "audio": {"bgm_path": "C:/music/old.mp3"}}),
        encoding="utf-8",
    )
    config, issues = ConfigManager(config_path=path, template_path=TEMPLATE).load_with_issues()
    assert config.audio.playlist == [{"path": "C:/music/old.mp3", "title": "old"}]
    assert config.audio.bgm_path == "C:/music/old.mp3"
    assert any("迁移" in item for item in issues)


def test_playlist_filters_and_deduplicates(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "audio": {
                    "playlist": [
                        {"path": "C:/a.mp3", "title": "A"},
                        {"path": "C:/a.mp3", "title": "重复"},
                        {"path": "", "title": "空"},
                        "C:/b.wav",
                    ],
                    "current_index": 5,
                },
            }
        ),
        encoding="utf-8",
    )
    config, _ = ConfigManager(config_path=path, template_path=TEMPLATE).load_with_issues()
    assert [track["path"] for track in config.audio.playlist] == ["C:/a.mp3", "C:/b.wav"]
    assert config.audio.current_index == 1, "越界的 current_index 应被收敛"


def test_playlist_operations():
    from rusty_moment.config.config_manager import AudioConfig

    audio = AudioConfig()
    assert audio.add_track("C:/a.mp3", "A") == 0
    assert audio.add_track("C:/b.mp3", "B") == 1
    assert audio.add_track("C:/a.mp3", "A") == 0, "重复路径不应重复添加"
    assert len(audio.playlist) == 2

    audio.set_current(1)
    assert audio.bgm_title == "B"
    assert audio.bgm_path == "C:/b.mp3"

    audio.set_current(99)
    assert audio.current_index == 1, "索引越界应被收敛"

    assert audio.move_track(1, -1) == 0
    assert audio.bgm_title == "B", "移动当前曲目时 current_index 应跟着走"

    audio.remove_track(0)
    assert len(audio.playlist) == 1
    assert audio.bgm_title == "A"


def test_empty_playlist_reports_empty_path():
    from rusty_moment.config.config_manager import AudioConfig

    audio = AudioConfig()
    assert audio.bgm_path == ""
    assert audio.bgm_title == ""
    audio.set_current(3)
    assert audio.current_index == 0


def test_alias_parsing_and_migration(tmp_path):
    """aliases 支持列表/逗号字符串，v2→v3 迁移给内置词补近义词但不覆盖用户写法。"""
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "keywords": {
                    "positives": [
                        {"phrase": "别管我", "weight": 1.0},
                        {"phrase": "我尽力了", "weight": 1.0, "aliases": ["我拼了"]},
                        {"phrase": "自定义词", "weight": 0.7, "aliases": "说法A，说法B"},
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    config, issues = ConfigManager(config_path=path, template_path=TEMPLATE).load_with_issues()
    assert config.version == CURRENT_CONFIG_VERSION

    by_phrase = {item["phrase"]: item for item in config.keywords.positives}
    # 内置词：自动补上默认近义词
    assert "别救我" in by_phrase["别管我"]["aliases"]
    # 用户自己写的近义词：原样保留，不被覆盖
    assert by_phrase["我尽力了"]["aliases"] == ["我拼了"]
    # 字符串形式的 aliases 也能解析
    assert by_phrase["自定义词"]["aliases"] == ["说法A", "说法B"]
    assert any("v3" in item for item in issues)


def test_alias_equal_to_phrase_is_dropped(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "version": 3,
                "keywords": {
                    "positives": [{"phrase": "别管我", "weight": 1.0, "aliases": ["别管我", "别救我"]}]
                },
            }
        ),
        encoding="utf-8",
    )
    config, _ = ConfigManager(config_path=path, template_path=TEMPLATE).load_with_issues()
    assert config.keywords.positives[0]["aliases"] == ["别救我"]


def test_obs_control_enabled_defaults_true(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"version": 2, "obs": {"host": "127.0.0.1"}}), encoding="utf-8")
    config, _ = ConfigManager(config_path=path, template_path=TEMPLATE).load_with_issues()
    assert config.obs.control_enabled is True, "默认仍是 OBS 控制模式"


def test_obs_control_enabled_can_be_disabled(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"version": 2, "obs": {"control_enabled": False}}), encoding="utf-8"
    )
    config, _ = ConfigManager(config_path=path, template_path=TEMPLATE).load_with_issues()
    assert config.obs.control_enabled is False


def test_default_config_discovers_three_tracks(tmp_path, monkeypatch):
    import rusty_moment.audio.music_library as library

    fake_tracks = [
        {"path": f"C:/music/{index}.aac", "title": f"曲目{index}"} for index in range(3)
    ]
    monkeypatch.setattr(library, "discover_tracks", lambda *a, **k: [dict(t) for t in fake_tracks])

    manager = ConfigManager(config_path=tmp_path / "config.json", template_path=TEMPLATE)
    config = manager.load()
    assert len(config.audio.playlist) == 3
    assert config.audio.bgm_title == "曲目0"


def test_saved_playlist_roundtrip(tmp_path):
    manager = make_manager(tmp_path)
    config = manager.load()
    config.audio.playlist = [
        {"path": "C:/music/a.mp3", "title": "第一首"},
        {"path": "C:/music/b.aac", "title": "第二首"},
    ]
    config.audio.current_index = 1
    manager.save(config)

    reloaded = make_manager(tmp_path).load()
    assert len(reloaded.audio.playlist) == 2
    assert reloaded.audio.current_index == 1
    assert reloaded.audio.bgm_title == "第二首"

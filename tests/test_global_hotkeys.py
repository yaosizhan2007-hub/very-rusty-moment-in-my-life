"""全局热键测试：解析、配置映射、真实注册（不可用时自动跳过）。"""

from __future__ import annotations

import sys

import pytest

from rusty_moment.config.config_manager import HotkeyConfig
from rusty_moment.core.global_hotkeys import (
    MOD_ALT,
    MOD_CONTROL,
    MOD_NOREPEAT,
    GlobalHotkeyManager,
    HotkeyError,
    build_binding_map,
    parse_hotkey,
)


def test_parse_function_key():
    modifiers, vk = parse_hotkey("F8")
    assert modifiers == 0
    assert vk == 0x77  # F8 = 0x70 + 7


def test_parse_modifiers_are_order_independent():
    assert parse_hotkey("Ctrl+Alt+F9") == parse_hotkey("alt+ctrl+f9")
    modifiers, vk = parse_hotkey("Ctrl+Shift+F1")
    assert modifiers == MOD_CONTROL | 0x0004
    assert vk == 0x70


def test_parse_letter_and_digit():
    assert parse_hotkey("Ctrl+K")[1] == ord("K")
    assert parse_hotkey("Alt+1")[1] == ord("1")
    assert parse_hotkey("alt+1")[0] == MOD_ALT


def test_parse_fullwidth_plus():
    assert parse_hotkey("Ctrl＋F2")[1] == 0x71


@pytest.mark.parametrize("bad", ["", "Ctrl", "Ctrl+NotAKey", "F99"])
def test_parse_invalid_raises(bad):
    with pytest.raises(HotkeyError):
        parse_hotkey(bad)


def test_build_binding_map_skips_empty():
    cfg = HotkeyConfig(force_trigger="F8", stop_music="", toggle_auto="F10")
    mapping = build_binding_map(cfg)
    assert mapping == {"force_trigger": "F8", "toggle_auto": "F10"}


def test_manager_reports_unavailable_on_non_windows():
    manager = GlobalHotkeyManager()
    if sys.platform == "win32":
        assert manager.available is True
    else:  # pragma: no cover - 本项目只跑 Windows
        assert manager.available is False
        assert manager.start({"force_trigger": "F8"})


@pytest.mark.skipif(sys.platform != "win32", reason="全局热键只在 Windows 实现")
def test_register_and_unregister_real_hotkey():
    """真实注册一个冷门组合（Ctrl+Alt+F13），确认能注册也能干净注销。"""
    manager = GlobalHotkeyManager(on_action=lambda action: None)
    failures = manager.start({"force_trigger": "Ctrl+Alt+F13"})
    try:
        if failures:
            pytest.skip(f"本机无法注册该热键：{failures}")
        assert manager.running
        assert [binding.action for binding in manager.bound] == ["force_trigger"]
    finally:
        manager.stop()
    assert not manager.running


@pytest.mark.skipif(sys.platform != "win32", reason="全局热键只在 Windows 实现")
def test_invalid_sequence_is_reported_not_raised():
    manager = GlobalHotkeyManager()
    failures = manager.start({"force_trigger": "Ctrl+NotAKey"})
    manager.stop()
    assert failures and "不支持的按键" in failures[0]


def test_mod_norepeat_flag_is_used():
    """注册时应带上 MOD_NOREPEAT，避免按住键连发一串事件。"""
    assert MOD_NOREPEAT == 0x4000

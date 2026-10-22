"""音频状态管理器测试：恢复正确性、用户干预、断线、异常退出快照。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fakes import FakeControl
from rusty_moment.audio.audio_state_manager import (
    AudioStateError,
    AudioStateManager,
    BusyStateError,
)
from rusty_moment.obs.obs_controller import AudioSourceState


def make_manager(control: FakeControl, tmp_path: Path, **kwargs) -> AudioStateManager:
    return AudioStateManager(control, persist_path=tmp_path / "pending.json", **kwargs)


def test_full_cycle_restores_original_state(tmp_path):
    control = FakeControl({"桌面音频": AudioSourceState("桌面音频", muted=False, volume_mul=0.7)})
    manager = make_manager(control, tmp_path)

    snapshot = manager.begin("evt-1", "桌面音频")
    assert snapshot.muted is False
    assert manager.is_active

    manager.apply_control("mute", 0.15)
    assert control.states["桌面音频"].muted is True

    outcome = manager.restore()
    assert outcome.ok
    assert control.states["桌面音频"].muted is False
    assert control.states["桌面音频"].volume_mul == pytest.approx(0.7)
    assert not manager.is_active
    assert not (tmp_path / "pending.json").exists()


def test_originally_muted_source_stays_muted(tmp_path):
    control = FakeControl({"桌面音频": AudioSourceState("桌面音频", muted=True, volume_mul=0.5)})
    manager = make_manager(control, tmp_path)

    manager.begin("evt-2", "桌面音频")
    manager.apply_control("mute", 0.15)
    outcome = manager.restore()

    assert outcome.ok
    assert control.states["桌面音频"].muted is True, "原本静音的音源不能被意外打开"
    assert control.states["桌面音频"].volume_mul == pytest.approx(0.5)


def test_multiple_events_rejected_while_active(tmp_path):
    control = FakeControl()
    manager = make_manager(control, tmp_path)
    manager.begin("evt-a", "桌面音频")
    with pytest.raises(BusyStateError):
        manager.begin("evt-b", "桌面音频")
    manager.release()


def test_user_manual_unmute_is_not_overwritten(tmp_path):
    control = FakeControl({"桌面音频": AudioSourceState("桌面音频", muted=False, volume_mul=0.8)})
    manager = make_manager(control, tmp_path)
    manager.begin("evt-3", "桌面音频")
    manager.apply_control("mute", 0.2)

    # 用户在事件期间自己把音源打开了
    control.simulate_user_mute("桌面音频", False)

    outcome = manager.restore()
    assert control.states["桌面音频"].muted is False
    assert outcome.skipped_user_modified
    assert not manager.is_active


def test_user_manual_volume_change_is_not_overwritten(tmp_path):
    control = FakeControl({"桌面音频": AudioSourceState("桌面音频", muted=False, volume_mul=0.8)})
    manager = make_manager(control, tmp_path)
    manager.begin("evt-4", "桌面音频")
    manager.apply_control("duck", 0.2)

    control.simulate_user_volume("桌面音频", 0.55)

    outcome = manager.restore()
    assert control.states["桌面音频"].volume_mul == pytest.approx(0.55)
    assert outcome.skipped_user_modified


def test_restore_when_obs_disconnected_keeps_snapshot(tmp_path):
    control = FakeControl()
    manager = make_manager(control, tmp_path)
    manager.begin("evt-5", "桌面音频")
    manager.apply_control("mute", 0.2)

    control.connected = False
    outcome = manager.restore()

    assert not outcome.ok
    assert outcome.snapshot_kept
    assert manager.is_active, "断线时必须保留快照，重连后还能恢复"

    control.connected = True
    outcome2 = manager.restore()
    assert outcome2.ok
    assert control.states["桌面音频"].muted is False


def test_begin_requires_connection_and_valid_source(tmp_path):
    control = FakeControl(connected=False)
    manager = make_manager(control, tmp_path)
    with pytest.raises(AudioStateError):
        manager.begin("evt-6", "桌面音频")

    control.connected = True
    with pytest.raises(AudioStateError):
        manager.begin("evt-6", "")


def test_begin_with_unknown_source(tmp_path):
    control = FakeControl()
    manager = make_manager(control, tmp_path)
    from rusty_moment.obs.obs_controller import OBSSourceError

    with pytest.raises(OBSSourceError):
        manager.begin("evt-7", "空音源")


def test_force_unmute_strategy(tmp_path):
    control = FakeControl({"桌面音频": AudioSourceState("桌面音频", muted=True, volume_mul=0.9)})
    manager = make_manager(control, tmp_path, restore_strategy="force_unmute")
    manager.begin("evt-8", "桌面音频")
    manager.apply_control("mute", 0.2)
    outcome = manager.restore()
    assert outcome.ok
    assert control.states["桌面音频"].muted is False


def test_snapshot_persisted_for_crash_recovery(tmp_path):
    control = FakeControl({"桌面音频": AudioSourceState("桌面音频", muted=False, volume_mul=0.7)})
    manager = make_manager(control, tmp_path)
    manager.begin("evt-9", "桌面音频")
    manager.apply_control("mute", 0.2)

    pending_file = tmp_path / "pending.json"
    assert pending_file.exists()
    data = json.loads(pending_file.read_text(encoding="utf-8"))
    assert data["event_id"] == "evt-9"
    assert data["snapshot"]["source"] == "桌面音频"

    loaded = AudioStateManager.load_pending(pending_file)
    assert loaded is not None
    assert loaded["snapshot"]["volume_mul"] == pytest.approx(0.7)

    manager.restore()
    assert not pending_file.exists()
    assert AudioStateManager.load_pending(pending_file) is None


def test_reset_to_snapshot_overrides_user_changes(tmp_path):
    control = FakeControl({"桌面音频": AudioSourceState("桌面音频", muted=False, volume_mul=0.7)})
    manager = make_manager(control, tmp_path)
    manager.begin("evt-10", "桌面音频")
    manager.apply_control("mute", 0.2)
    control.simulate_user_mute("桌面音频", False)

    outcome = manager.reset_to_snapshot()
    assert outcome.ok
    assert control.states["桌面音频"].muted is False
    assert control.states["桌面音频"].volume_mul == pytest.approx(0.7)


def test_restore_without_snapshot_is_noop(tmp_path):
    control = FakeControl()
    manager = make_manager(control, tmp_path)
    outcome = manager.restore()
    assert outcome.ok
    assert outcome.restored == []

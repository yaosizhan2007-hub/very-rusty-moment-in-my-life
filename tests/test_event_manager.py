"""事件管理器集成测试（使用替身播放器与替身 OBS 控制）。

覆盖：手动触发全流程、播放中重复触发、停止、OBS 断开降级、播放失败回滚、
全局静音、自动触发开关、排队策略。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from fakes import FakeControl, FakePlayer
from rusty_moment.audio.audio_player import AudioFileError
from rusty_moment.audio.audio_state_manager import AudioStateManager
from rusty_moment.config.config_manager import AppConfig
from rusty_moment.core.cooldown_manager import CooldownManager
from rusty_moment.core.event_manager import EventManager, EventRecord
from rusty_moment.core.state_machine import EventState, StateMachine
from rusty_moment.obs.obs_controller import AudioSourceState


class Harness:
    def __init__(
        self,
        tmp_path: Path,
        *,
        player: FakePlayer | None = None,
        control: FakeControl | None = None,
        config: AppConfig | None = None,
    ) -> None:
        self.config = config or AppConfig()
        self.config.audio.playlist = [{"path": "C:/music/rusty.mp3", "title": "rusty"}]
        self.config.audio.current_index = 0
        self.config.obs.game_source = "桌面音频"
        self.config.trigger.cooldown_seconds = 0.0
        self.player = player or FakePlayer(duration=0.12)
        self.control = control or FakeControl(
            {"桌面音频": AudioSourceState("桌面音频", muted=False, volume_mul=0.8)}
        )
        self.state_manager = AudioStateManager(
            self.control, persist_path=tmp_path / "pending.json"
        )
        self.machine = StateMachine()
        self.cooldown = CooldownManager()
        self.finished: list[EventRecord] = []
        self.started: list[EventRecord] = []
        from rusty_moment.core.event_manager import EventCallbacks

        self.events = EventManager(
            player=self.player,
            control=self.control,
            state_manager=self.state_manager,
            state_machine=self.machine,
            cooldown=self.cooldown,
            config_provider=lambda: self.config,
            callbacks=EventCallbacks(
                on_event_started=self.started.append,
                on_event_finished=self.finished.append,
            ),
        )

    def wait_idle(self, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.events.is_busy:
                return
            time.sleep(0.01)
        raise AssertionError("事件未在预期时间内结束")


def test_manual_trigger_plays_and_restores(tmp_path):
    harness = Harness(tmp_path)
    result = harness.events.trigger_manual()

    assert result.started
    harness.wait_idle()

    assert harness.player.calls, "音乐应当被播放"
    assert harness.control.calls, "游戏音源应当被控制"
    assert harness.control.states["桌面音频"].muted is False, "结束后必须恢复"
    record = harness.finished[-1]
    assert record.played_ok is True
    assert record.restore_ok is True
    assert record.obs_controlled is True
    assert record.latency_ms is not None


def test_second_trigger_while_playing_is_ignored(tmp_path):
    harness = Harness(tmp_path)
    harness.player.duration = 0.4
    harness.events.trigger_manual()
    time.sleep(0.05)

    second = harness.events.trigger_manual()
    assert second.started is False
    assert "忽略" in second.message or "进行中" in second.message

    harness.wait_idle()
    assert len(harness.player.calls) == 1


def test_stop_restores_audio(tmp_path):
    harness = Harness(tmp_path)
    harness.player.duration = 5.0
    harness.events.trigger_manual()
    time.sleep(0.1)
    assert harness.control.states["桌面音频"].muted is True

    assert harness.events.stop() is True
    harness.wait_idle()

    assert harness.control.states["桌面音频"].muted is False
    assert harness.finished[-1].restore_ok is True


def test_obs_disconnected_falls_back_to_local_playback(tmp_path):
    control = FakeControl(connected=False)
    harness = Harness(tmp_path, control=control)
    harness.events.trigger_manual()
    harness.wait_idle()

    record = harness.finished[-1]
    assert harness.player.calls, "OBS 不在线时仍应本地播放"
    assert record.obs_controlled is False
    assert "OBS 未连接" in record.note


def test_playback_failure_still_restores_audio(tmp_path):
    player = FakePlayer(fail_with=AudioFileError("音乐文件不存在"))
    harness = Harness(tmp_path, player=player)
    harness.events.trigger_manual()
    harness.wait_idle()

    record = harness.finished[-1]
    assert record.played_ok is False
    assert harness.control.states["桌面音频"].muted is False, "播放失败也必须回滚静音"
    assert harness.machine.state in (EventState.COOLDOWN, EventState.IDLE)


def test_global_mute_blocks_manual_trigger(tmp_path):
    harness = Harness(tmp_path)
    harness.config.mode.global_mute = True
    result = harness.events.trigger_manual()
    assert result.started is False
    assert "全局静音" in result.message
    assert not harness.player.calls


def test_auto_trigger_respects_toggle_but_manual_still_works(tmp_path):
    harness = Harness(tmp_path)
    harness.config.mode.auto_trigger_enabled = False

    auto = harness.events.start_event("keyword", "命中「别管我」", score=0.8)
    assert auto.started is False

    manual = harness.events.trigger_manual()
    assert manual.started is True
    harness.wait_idle()


def test_queue_policy_runs_second_event_after_finish(tmp_path):
    harness = Harness(tmp_path)
    harness.config.trigger.busy_policy = "queue"
    harness.player.duration = 0.2

    harness.events.trigger_manual()
    time.sleep(0.05)
    queued = harness.events.trigger_manual()
    assert queued.queued is True

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and len(harness.player.calls) < 2:
        time.sleep(0.01)
    harness.wait_idle()
    assert len(harness.player.calls) >= 2, "排队的事件应在第一个结束后执行"


def test_cooldown_started_after_event(tmp_path):
    harness = Harness(tmp_path)
    harness.config.trigger.cooldown_seconds = 30.0
    harness.events.trigger_manual()
    harness.wait_idle()
    assert harness.cooldown.is_active()
    assert harness.cooldown.remaining() > 25.0


def test_skip_candidate_returns_to_idle(tmp_path):
    harness = Harness(tmp_path)
    harness.machine.transition(EventState.CANDIDATE, "候选")
    assert harness.events.skip_candidate() is True
    assert harness.machine.state == EventState.IDLE
    assert harness.events.skip_candidate() is False


def test_music_loop_after_stop_does_not_restart(tmp_path):
    harness = Harness(tmp_path)
    harness.player.duration = 0.1
    harness.events.trigger_manual()
    harness.wait_idle()
    first = len(harness.player.calls)
    time.sleep(0.15)
    assert len(harness.player.calls) == first, "事件结束后不应自动重启音乐"


def test_record_reason_and_score_propagate(tmp_path):
    harness = Harness(tmp_path)
    harness.events.start_event("keyword", "命中「我尽力了」", score=0.83)
    harness.wait_idle()
    record = harness.finished[-1]
    assert record.score == pytest.approx(0.83)
    assert "我尽力了" in record.reason
    assert record.mode == "serious"


def test_local_only_mode_skips_obs_control(tmp_path):
    """纯本地模式：只放音乐，完全不碰 OBS 音源。"""
    harness = Harness(tmp_path)
    harness.config.obs.control_enabled = False

    harness.events.trigger_manual()
    harness.wait_idle()

    assert harness.player.calls, "纯本地模式仍应播放音乐"
    assert not harness.control.calls, "纯本地模式不应读/写任何 OBS 音源"
    record = harness.finished[-1]
    assert record.obs_controlled is False
    assert "纯本地" in record.note


def test_local_only_mode_survives_disconnected_obs(tmp_path):
    control = FakeControl(connected=False)
    harness = Harness(tmp_path, control=control)
    harness.config.obs.control_enabled = False

    ok, message = harness.events.test_audio(duration=0.05)

    assert ok, message
    assert "纯本地" in message

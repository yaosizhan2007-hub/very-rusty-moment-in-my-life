"""状态机与冷却计时器测试。"""

from __future__ import annotations

import pytest

from rusty_moment.core.cooldown_manager import CooldownManager
from rusty_moment.core.state_machine import (
    EventState,
    IllegalTransition,
    StateMachine,
)


def test_happy_path_transitions():
    changes = []
    machine = StateMachine(on_change=changes.append)

    machine.transition(EventState.CANDIDATE, "候选")
    machine.transition(EventState.TRIGGERED, "触发")
    machine.transition(EventState.PLAYING, "播放")
    machine.transition(EventState.RESTORING, "恢复")
    machine.transition(EventState.COOLDOWN, "冷却")
    machine.transition(EventState.IDLE, "就绪")

    assert machine.state == EventState.IDLE
    assert [change.new for change in changes] == [
        EventState.CANDIDATE,
        EventState.TRIGGERED,
        EventState.PLAYING,
        EventState.RESTORING,
        EventState.COOLDOWN,
        EventState.IDLE,
    ]


@pytest.mark.parametrize(
    "start, target",
    [
        (EventState.IDLE, EventState.PLAYING),
        (EventState.IDLE, EventState.RESTORING),
        (EventState.PLAYING, EventState.COOLDOWN),
        (EventState.PLAYING, EventState.IDLE),
        (EventState.CANDIDATE, EventState.RESTORING),
    ],
)
def test_illegal_transitions_raise(start, target):
    machine = StateMachine()
    if start == EventState.PLAYING:
        machine.transition(EventState.TRIGGERED, "触发")
        machine.transition(EventState.PLAYING, "播放")
    elif start == EventState.CANDIDATE:
        machine.transition(EventState.CANDIDATE, "候选")
    with pytest.raises(IllegalTransition):
        machine.transition(target, "非法")


def test_manual_trigger_can_skip_candidate():
    machine = StateMachine()
    machine.transition(EventState.TRIGGERED, "手动强制触发")
    assert machine.state == EventState.TRIGGERED


def test_force_reset_from_any_state():
    machine = StateMachine()
    machine.transition(EventState.TRIGGERED, "触发")
    machine.transition(EventState.PLAYING, "播放")
    machine.force_reset("OBS 断线")
    assert machine.state == EventState.IDLE


def test_failed_playback_can_go_back_to_idle():
    machine = StateMachine()
    machine.transition(EventState.TRIGGERED, "触发")
    machine.transition(EventState.IDLE, "文件损坏，什么都没发生")
    assert machine.state == EventState.IDLE


def test_callback_exception_does_not_break_state():
    def boom(_change):
        raise RuntimeError("callback error")

    machine = StateMachine(on_change=boom)
    machine.transition(EventState.TRIGGERED, "触发")
    assert machine.state == EventState.TRIGGERED


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_cooldown_counts_down():
    clock = FakeClock()
    cooldown = CooldownManager(clock=clock)
    assert cooldown.is_ready()

    cooldown.start(30.0)
    assert cooldown.is_active()
    assert cooldown.remaining() == pytest.approx(30.0)

    clock.advance(10.0)
    assert cooldown.remaining() == pytest.approx(20.0)
    assert not cooldown.is_ready()

    clock.advance(20.0)
    assert cooldown.is_ready()
    assert cooldown.remaining() == 0.0


def test_cooldown_stop_and_zero_duration():
    clock = FakeClock()
    cooldown = CooldownManager(clock=clock)
    cooldown.start(5.0)
    cooldown.stop()
    assert cooldown.is_ready()

    cooldown.start(0.0)
    assert cooldown.is_ready()

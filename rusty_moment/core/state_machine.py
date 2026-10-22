"""事件状态机：``IDLE → CANDIDATE → TRIGGERED → PLAYING → RESTORING → COOLDOWN → IDLE``。

状态转移表是「白名单」式的：没写在表里的转移一律抛 :class:`IllegalTransition`，
避免业务代码靠散落的 if 判断把状态搞乱。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable

log = logging.getLogger(__name__)


class EventState(str, Enum):
    IDLE = "idle"
    CANDIDATE = "candidate"
    TRIGGERED = "triggered"
    PLAYING = "playing"
    RESTORING = "restoring"
    COOLDOWN = "cooldown"


ALLOWED_TRANSITIONS: dict[EventState, set[EventState]] = {
    EventState.IDLE: {EventState.CANDIDATE, EventState.TRIGGERED},
    EventState.CANDIDATE: {EventState.IDLE, EventState.TRIGGERED},
    # TRIGGERED 可以直接回 IDLE：OBS 断了/文件坏了，什么都没发生
    EventState.TRIGGERED: {EventState.PLAYING, EventState.RESTORING, EventState.IDLE},
    EventState.PLAYING: {EventState.RESTORING},
    EventState.RESTORING: {EventState.COOLDOWN, EventState.IDLE},
    EventState.COOLDOWN: {EventState.IDLE, EventState.TRIGGERED, EventState.CANDIDATE},
}

STATE_LABELS = {
    EventState.IDLE: "空闲",
    EventState.CANDIDATE: "候选事件",
    EventState.TRIGGERED: "已触发",
    EventState.PLAYING: "播放中",
    EventState.RESTORING: "恢复音频",
    EventState.COOLDOWN: "冷却中",
}


class IllegalTransition(Exception):
    """非法的状态转移。"""


@dataclass(frozen=True)
class StateChange:
    old: EventState
    new: EventState
    reason: str
    at: float


class StateMachine:
    """带监听回调与历史记录的事件状态机（线程安全）。"""

    def __init__(
        self,
        on_change: Callable[[StateChange], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._state = EventState.IDLE
        self._on_change = on_change
        self._clock = clock
        self._lock = threading.RLock()
        self.history: list[StateChange] = []

    @property
    def state(self) -> EventState:
        return self._state

    @property
    def label(self) -> str:
        return STATE_LABELS[self._state]

    def can_transition(self, target: EventState) -> bool:
        if target == self._state:
            return True
        return target in ALLOWED_TRANSITIONS.get(self._state, set())

    def transition(self, target: EventState, reason: str = "") -> StateChange:
        with self._lock:
            current = self._state
            if target == current:
                return StateChange(current, target, reason or "重复设置", self._clock())
            if not self.can_transition(target):
                raise IllegalTransition(
                    f"非法状态转移：{current.value} → {target.value}（{reason}）"
                )
            change = StateChange(current, target, reason, self._clock())
            self._state = target
            self.history.append(change)
            if len(self.history) > 200:
                del self.history[: len(self.history) - 200]
        log.debug("状态机 %s → %s（%s）", change.old.value, change.new.value, reason)
        if self._on_change is not None:
            try:
                self._on_change(change)
            except Exception:  # pragma: no cover - 回调异常不影响状态机
                log.exception("状态机回调异常")
        return change

    def force_reset(self, reason: str = "强制复位") -> StateChange:
        """异常路径：无论当前什么状态都回到 IDLE。"""
        with self._lock:
            change = StateChange(self._state, EventState.IDLE, reason, self._clock())
            self._state = EventState.IDLE
            self.history.append(change)
        log.warning("状态机强制复位：%s → idle（%s）", change.old.value, reason)
        if self._on_change is not None:
            try:
                self._on_change(change)
            except Exception:  # pragma: no cover
                log.exception("状态机回调异常")
        return change

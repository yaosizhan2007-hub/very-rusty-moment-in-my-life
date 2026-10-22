"""冷却计时器。

冷却从「事件结束」开始算（不是从触发开始），这样音乐放完立刻就再来一次
也不会把频率放大。clock 可注入，测试里用假时钟就能验证边界。
"""

from __future__ import annotations

import threading
import time
from typing import Callable


class CooldownManager:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.RLock()
        self._until = 0.0
        self._duration = 0.0

    def start(self, duration: float) -> None:
        with self._lock:
            self._duration = max(0.0, float(duration))
            self._until = self._clock() + self._duration

    def stop(self) -> None:
        with self._lock:
            self._until = 0.0
            self._duration = 0.0

    @property
    def duration(self) -> float:
        return self._duration

    def remaining(self) -> float:
        with self._lock:
            return max(0.0, self._until - self._clock())

    def is_active(self) -> bool:
        return self.remaining() > 0.0

    def is_ready(self) -> bool:
        return not self.is_active()

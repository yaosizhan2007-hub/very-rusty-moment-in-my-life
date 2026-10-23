"""时间窗口上下文跟踪：给组合规则提供「最近说过什么」的证据。

约束（对应规格书第五节「上下文反转」与第八节）：
- 只保留内存里的最近若干句，不落盘、不写日志全文；
- 窗口按单调时钟计算，跨句子的判断有明确的时间上限；
- 每个事件只存类别/标签/词条 id 与分数，不存原始音频。
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable


@dataclass(frozen=True)
class ContextEvent:
    at: float
    text_len: int
    categories: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    entry_ids: tuple[str, ...] = ()
    score: float = 0.0


@dataclass
class ContextTracker:
    window_seconds: float = 15.0
    max_events: int = 16
    clock: Callable[[], float] = time.monotonic
    _events: deque[ContextEvent] = field(default_factory=deque)

    def __post_init__(self) -> None:
        if not isinstance(self._events, deque):
            self._events = deque()
        self._events = deque(self._events, maxlen=max(1, int(self.max_events)))

    # -- 写入 --------------------------------------------------------------- #
    def record(
        self,
        *,
        text: str = "",
        categories: list[str] | tuple[str, ...] = (),
        tags: list[str] | tuple[str, ...] = (),
        entry_ids: list[str] | tuple[str, ...] = (),
        score: float = 0.0,
    ) -> ContextEvent:
        event = ContextEvent(
            at=self.clock(),
            text_len=len(text or ""),
            categories=tuple(categories),
            tags=tuple(tags),
            entry_ids=tuple(entry_ids),
            score=float(score),
        )
        self._events.append(event)
        return event

    def reset(self) -> None:
        self._events.clear()

    # -- 读取 --------------------------------------------------------------- #
    def recent(self, window_seconds: float | None = None) -> list[ContextEvent]:
        window = self.window_seconds if window_seconds is None else max(0.0, window_seconds)
        deadline = self.clock() - window
        return [event for event in self._events if event.at >= deadline]

    def has_category(self, category: str, window_seconds: float) -> bool:
        return any(category in event.categories for event in self.recent(window_seconds))

    def has_tag(self, tag: str, window_seconds: float) -> bool:
        return any(tag in event.tags for event in self.recent(window_seconds))

    def has_entry(self, entry_id: str, window_seconds: float) -> bool:
        return any(entry_id in event.entry_ids for event in self.recent(window_seconds))

    def summary(self, window_seconds: float | None = None) -> str:
        events = self.recent(window_seconds)
        if not events:
            return "上下文：空"
        categories: list[str] = []
        for event in events:
            for category in event.categories:
                if category not in categories:
                    categories.append(category)
        return f"上下文：最近 {len(events)} 句，涉及 {('、'.join(categories)) or '无类别'}"

"""Qt 与后台线程之间的小桥。

规则：GUI 线程只做界面，耗时的 OBS 连接、音频测试、事件流程都在 worker 线程里；
worker 通过 ``Signal`` 回到 GUI 线程（Qt 默认跨线程队列连接），不在 worker 里碰控件。
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from PySide6.QtCore import QObject, Signal

log = logging.getLogger(__name__)


class AsyncCall(QObject):
    """一次性异步调用载体。"""

    succeeded = Signal(object)
    failed = Signal(str)


def run_async(
    parent: QObject,
    label: str,
    fn: Callable[[], Any],
    on_success: Callable[[Any], None] | None = None,
    on_error: Callable[[str], None] | None = None,
) -> AsyncCall:
    call = AsyncCall(parent)

    def handle_success(result: Any) -> None:
        try:
            if on_success is not None:
                on_success(result)
        finally:
            call.deleteLater()

    def handle_error(message: str) -> None:
        try:
            if on_error is not None:
                on_error(message)
            else:
                log.error("%s 失败：%s", label, message)
        finally:
            call.deleteLater()

    call.succeeded.connect(handle_success)
    call.failed.connect(handle_error)

    def worker() -> None:
        try:
            result = fn()
        except Exception as exc:  # 包含 OBS/音频/IO 异常
            log.warning("%s 失败：%s", label, exc)
            call.failed.emit(str(exc))
        else:
            call.succeeded.emit(result)

    threading.Thread(target=worker, name=f"ui-task-{label}", daemon=True).start()
    return call


class EventBridge(QObject):
    """把 EventManager / StateMachine 的回调搬到 GUI 线程。"""

    state_changed = Signal(object)  # StateChange
    event_started = Signal(object)  # EventRecord
    event_finished = Signal(object)  # EventRecord
    log_message = Signal(str, str)  # level, message
    connection_changed = Signal(bool, str)  # connected, detail
    detection_status = Signal(object)  # DetectionStatus
    listener_state = Signal(bool, str)  # running, detail
    global_hotkey = Signal(str)  # action: force_trigger / stop_music / toggle_auto

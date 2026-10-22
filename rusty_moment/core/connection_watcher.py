"""OBS 连接守护线程：断线后按配置自动重连。

放在独立线程里是因为连接本身是阻塞的（最长 timeout 秒），绝不能占用 GUI 线程。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

log = logging.getLogger(__name__)


class ConnectionWatcher:
    def __init__(
        self,
        connect_fn: Callable[[], str],
        is_connected: Callable[[], bool],
        *,
        enabled: Callable[[], bool],
        interval: Callable[[], float],
        on_connected: Callable[[str], None] | None = None,
        on_failed: Callable[[str], None] | None = None,
        poll_interval: float = 1.0,
    ) -> None:
        self._connect = connect_fn
        self._is_connected = is_connected
        self._enabled = enabled
        self._interval = interval
        self._on_connected = on_connected
        self._on_failed = on_failed
        self._poll = poll_interval
        self._stop = threading.Event()
        self._connecting = threading.Lock()
        self._thread: threading.Thread | None = None
        self._last_attempt = 0.0
        self._failed_once = False

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="obs-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._thread = None

    def _loop(self) -> None:
        # 启动时先给 OBS 一次自动连接的机会
        while not self._stop.wait(self._poll):
            try:
                if not self._enabled():
                    continue
                if self._is_connected():
                    self._failed_once = False
                    continue
                now = time.monotonic()
                if now - self._last_attempt < max(1.0, self._interval()):
                    continue
                if not self._connecting.acquire(blocking=False):
                    continue
                self._last_attempt = now
                try:
                    info = self._connect()
                except Exception as exc:
                    if not self._failed_once:
                        log.warning("自动重连失败：%s", exc)
                        if self._on_failed is not None:
                            self._on_failed(str(exc))
                        self._failed_once = True
                else:
                    log.info("自动重连成功：%s", info)
                    self._failed_once = False
                    if self._on_connected is not None:
                        self._on_connected(info)
                finally:
                    self._connecting.release()
            except Exception:  # pragma: no cover - 守护线程不能死
                log.exception("连接守护线程异常")

"""全局热键（可选，默认关闭）。

为什么默认关闭：``RegisterHotKey`` 是**系统级独占**的——注册 F8 之后，游戏里按 F8
就收不到这个键了。提示词里明确要求「不要默认注册会干扰游戏操作的全局按键」，
所以这里只在用户主动开启时才注册，并且在 UI 上写清楚风险。

实现：单独的线程里 RegisterHotKey + GetMessage 循环（Windows 的消息队列按线程绑定），
回调在消息线程里触发，调用方负责转回自己的线程（GUI 用 Qt 信号）。
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
from dataclasses import dataclass
from typing import Callable

log = logging.getLogger(__name__)

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

_FUNCTION_KEYS = {f"F{index}": 0x70 + index - 1 for index in range(1, 25)}


class HotkeyError(Exception):
    """热键字符串无法解析。"""


@dataclass(frozen=True)
class HotkeyBinding:
    action: str
    sequence: str
    modifiers: int
    vk: int


def parse_hotkey(sequence: str) -> tuple[int, int]:
    """把 ``Ctrl+Alt+F8`` 解析成 (modifiers, virtual-key)。"""
    text = (sequence or "").strip()
    if not text:
        raise HotkeyError("空的热键")
    parts = [part.strip() for part in text.replace("＋", "+").split("+") if part.strip()]
    if not parts:
        raise HotkeyError(f"无法解析热键：{sequence}")
    modifiers = 0
    key = ""
    for part in parts:
        lowered = part.lower()
        if lowered in ("ctrl", "control"):
            modifiers |= MOD_CONTROL
        elif lowered == "alt":
            modifiers |= MOD_ALT
        elif lowered == "shift":
            modifiers |= MOD_SHIFT
        elif lowered in ("win", "meta", "super"):
            modifiers |= MOD_WIN
        else:
            key = part.upper()
    if not key:
        raise HotkeyError(f"热键缺少主键：{sequence}")
    if key in _FUNCTION_KEYS:
        vk = _FUNCTION_KEYS[key]
    elif len(key) == 1 and (key.isalnum()):
        vk = ord(key)
    elif key in ("SPACE", "TAB", "ESC", "ESCAPE"):
        vk = {"SPACE": 0x20, "TAB": 0x09, "ESC": 0x1B, "ESCAPE": 0x1B}[key]
    else:
        raise HotkeyError(f"不支持的按键：{key}")
    return modifiers, vk


class GlobalHotkeyManager:
    """注册一组全局热键并在独立线程里派发。"""

    def __init__(self, on_action: Callable[[str], None] | None = None) -> None:
        self._on_action = on_action
        self._bindings: list[HotkeyBinding] = []
        self._ids: dict[int, str] = {}
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self._ready = threading.Event()
        self._registered = threading.Event()
        self._stop = threading.Event()
        self._failures: list[str] = []
        self._user32 = ctypes.windll.user32 if sys.platform == "win32" else None
        self._kernel32 = ctypes.windll.kernel32 if sys.platform == "win32" else None

    # -- 查询 --------------------------------------------------------------- #
    @property
    def available(self) -> bool:
        return self._user32 is not None

    @property
    def running(self) -> bool:
        thread = self._thread
        return bool(thread and thread.is_alive())

    @property
    def failures(self) -> list[str]:
        return list(self._failures)

    @property
    def bound(self) -> list[HotkeyBinding]:
        return list(self._bindings)

    # -- 生命周期 ----------------------------------------------------------- #
    def start(self, mapping: dict[str, str], timeout: float = 2.0) -> list[str]:
        """注册 ``{action: sequence}``，返回注册失败的条目说明。"""
        if not self.available:
            return ["当前平台不支持全局热键"]
        self.stop()
        bindings: list[HotkeyBinding] = []
        failures: list[str] = []
        for action, sequence in mapping.items():
            if not sequence:
                continue
            try:
                modifiers, vk = parse_hotkey(sequence)
            except HotkeyError as exc:
                failures.append(f"{action}: {exc}")
                continue
            bindings.append(
                HotkeyBinding(action=action, sequence=sequence, modifiers=modifiers, vk=vk)
            )
        if not bindings:
            self._failures = failures
            return failures

        self._bindings = bindings
        self._failures = failures
        self._ready.clear()
        self._registered.clear()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="global-hotkeys", daemon=True)
        self._thread.start()
        self._ready.wait(timeout)
        # 注册动作在消息线程里做，失败信息通过 _failures 回传
        self._registered.wait(timeout)
        return self._failures

    def stop(self) -> None:
        thread, self._thread = self._thread, None
        if thread is None or not thread.is_alive():
            self._bindings = []
            return
        self._stop.set()
        try:
            if self._thread_id:
                self._user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        except Exception:  # pragma: no cover
            pass
        thread.join(timeout=2.0)
        self._bindings = []
        self._ids.clear()
        self._thread_id = None

    # -- 内部 --------------------------------------------------------------- #
    def _run(self) -> None:
        from ctypes import wintypes

        user32 = self._user32
        self._thread_id = int(self._kernel32.GetCurrentThreadId())
        self._ids.clear()
        next_id = 1
        for binding in self._bindings:
            hotkey_id = next_id
            next_id += 1
            ok = user32.RegisterHotKey(
                None, hotkey_id, binding.modifiers | MOD_NOREPEAT, binding.vk
            )
            if ok:
                self._ids[hotkey_id] = binding.action
            else:
                self._failures.append(
                    f"{binding.action}: 注册 {binding.sequence} 失败（可能被其它程序占用）"
                )
        self._ready.set()
        self._registered.set()

        try:
            message = wintypes.MSG()
            while not self._stop.is_set():
                result = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result == 0 or result == -1:
                    break
                if message.message == WM_HOTKEY:
                    action = self._ids.get(int(message.wParam))
                    if action and self._on_action is not None:
                        try:
                            self._on_action(action)
                        except Exception:  # pragma: no cover - 回调异常不能打死线程
                            log.exception("全局热键回调异常：%s", action)
                else:
                    user32.TranslateMessage(ctypes.byref(message))
                    user32.DispatchMessageW(ctypes.byref(message))
        finally:
            for hotkey_id in list(self._ids):
                try:
                    user32.UnregisterHotKey(None, hotkey_id)
                except Exception:  # pragma: no cover
                    pass
            self._ids.clear()
            log.info("全局热键已注销")


def build_binding_map(hotkeys: object) -> dict[str, str]:
    """从 HotkeyConfig 生成 ``{action: sequence}``（只取启用且非空的项）。"""
    mapping = {
        "force_trigger": getattr(hotkeys, "force_trigger", ""),
        "stop_music": getattr(hotkeys, "stop_music", ""),
        "toggle_auto": getattr(hotkeys, "toggle_auto", ""),
    }
    return {action: sequence for action, sequence in mapping.items() if sequence}

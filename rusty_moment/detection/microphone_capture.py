"""麦克风采集：设备枚举、流式采集、可靠释放。

音频回调里只做 ``queue.put_nowait``（以及队列满时丢弃最旧块），
绝不做识别、IO 或 GUI 操作——这是提示词里写死的红线。
"""

from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass
from typing import Any, Callable

import numpy as np

log = logging.getLogger(__name__)


class MicrophoneError(Exception):
    """设备不可用 / 打开失败。"""


# 这些"输入设备"其实是系统输出的回环：会把队友语音、游戏音效一起录进来
LOOPBACK_HINTS = (
    "立体声混音",
    "stereo mix",
    "what u hear",
    "wave out mix",
    "loopback",
    "with sst",
    "monitor of",
)


def is_loopback_device_name(name: str) -> bool:
    """判断某个输入设备是不是"系统输出回环"（会录到队友说话/游戏声音）。"""
    lowered = (name or "").lower()
    return any(hint in lowered for hint in LOOPBACK_HINTS)


@dataclass(frozen=True)
class InputDevice:
    index: int
    name: str
    channels: int
    default_samplerate: int
    is_loopback: bool = False
    """True = 立体声混音/回环设备：采集到的是系统输出，包含队友语音。"""


class MicrophoneCapture:
    """把麦克风音频塞进一个有界队列，由工作线程消费。"""

    def __init__(
        self,
        sd_module: Any | None = None,
        *,
        max_queue_blocks: int = 64,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        # 延迟导入 sounddevice：构造阶段不碰 PortAudio，方便在无设备环境下构建整个应用
        self._sd = sd_module
        self._queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=max_queue_blocks)
        self._stream: Any = None
        self._lock = threading.RLock()
        self._on_error = on_error
        self._dropped_blocks = 0
        self._last_status = ""
        self.device: int | None = None
        self.device_name = ""
        self.is_loopback = False
        self.samplerate = 0
        self.blocksize = 0

    def _backend(self) -> Any:
        if self._sd is None:
            try:
                import sounddevice as sd  # type: ignore

                self._sd = sd
            except Exception as exc:  # pragma: no cover
                raise MicrophoneError(f"无法加载音频后端（PortAudio）：{exc}") from exc
        return self._sd

    # -- 设备 --------------------------------------------------------------- #
    def list_input_devices(self) -> list[InputDevice]:
        devices: list[InputDevice] = []
        try:
            raw = self._backend().query_devices()
        except Exception as exc:  # pragma: no cover
            log.warning("枚举输入设备失败：%s", exc)
            return devices
        for index, dev in enumerate(raw):
            channels = int(dev.get("max_input_channels", 0))
            if channels <= 0:
                continue
            devices.append(
                InputDevice(
                    index=index,
                    name=str(dev.get("name", f"设备 {index}")),
                    channels=channels,
                    default_samplerate=int(dev.get("default_samplerate", 48000)),
                    is_loopback=is_loopback_device_name(str(dev.get("name", ""))),
                )
            )
        return devices

    def resolve_device(self, spec: str | int | None) -> int | None:
        """把配置里的设备（索引或名称子串）解析成 sounddevice 索引。"""
        if spec is None or spec == "":
            return None
        if isinstance(spec, int):
            return spec
        text = str(spec).strip()
        devices = self.list_input_devices()
        if text.isdigit():
            index = int(text)
            if any(device.index == index for device in devices):
                return index
            raise MicrophoneError(f"输入设备索引 {index} 不可用")
        lowered = text.lower()
        for device in devices:
            if lowered in device.name.lower():
                return device.index
        raise MicrophoneError(f"找不到麦克风设备：{text}")

    # -- 采集 --------------------------------------------------------------- #
    @property
    def is_running(self) -> bool:
        stream = self._stream
        return bool(stream is not None and getattr(stream, "active", False))

    @property
    def dropped_blocks(self) -> int:
        return self._dropped_blocks

    def start(
        self,
        device: str | int | None = None,
        *,
        samplerate: int = 16000,
        blocksize: int = 480,
        channels: int = 1,
    ) -> None:
        with self._lock:
            if self.is_running:
                self.stop()
            index = self.resolve_device(device)
            target_name = ""
            try:
                if index is not None:
                    target_name = str(self._backend().query_devices(index).get("name", ""))
                else:
                    target_name = str(self._backend().query_devices(kind="input").get("name", ""))
            except Exception:  # pragma: no cover - 某些驱动不实现查询
                target_name = ""
            self.device_name = target_name
            self.is_loopback = is_loopback_device_name(target_name)
            if self.is_loopback:
                log.warning(
                    "当前麦克风设备是「%s」，这是系统输出回环，会录到队友语音与游戏声音",
                    target_name,
                )
            try:
                stream = self._backend().InputStream(
                    samplerate=int(samplerate),
                    channels=int(channels),
                    dtype="float32",
                    blocksize=int(blocksize),
                    device=index,
                    callback=self._callback,
                )
                stream.start()
            except Exception as exc:
                raise MicrophoneError(f"打开麦克风失败：{exc}") from exc
            self._stream = stream
            self.device = index
            self.samplerate = int(samplerate)
            self.blocksize = int(blocksize)
            self._clear_queue()
            log.info("麦克风已启动（device=%s, %dHz）", index, samplerate)

    def stop(self) -> None:
        with self._lock:
            stream, self._stream = self._stream, None
            if stream is None:
                return
            try:
                stream.stop()
            except Exception as exc:  # pragma: no cover
                log.warning("停止麦克风时出错：%s", exc)
            try:
                stream.close()
            except Exception:  # pragma: no cover
                pass
            log.info("麦克风已释放")

    def read(self, timeout: float = 0.5) -> np.ndarray | None:
        """取一块音频；超时返回 None（工作线程用它做循环）。"""
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def clear(self) -> None:
        self._clear_queue()

    # -- 内部 --------------------------------------------------------------- #
    def _clear_queue(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return

    def _callback(self, indata: Any, frames: int, time_info: Any, status: Any) -> None:
        if status:
            message = str(status)
            if message != self._last_status:
                self._last_status = message
                log.debug("麦克风回调状态：%s", message)
                if self._on_error is not None:
                    try:
                        self._on_error(message)
                    except Exception:  # pragma: no cover
                        pass
        block = np.array(indata[:, 0] if indata.ndim > 1 else indata, dtype=np.float32, copy=True)
        try:
            self._queue.put_nowait(block)
        except queue.Full:
            # 宁可丢旧数据，也不让回调阻塞（阻塞会直接导致爆音/丢音）
            self._dropped_blocks += 1
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(block)
            except queue.Empty:  # pragma: no cover
                pass

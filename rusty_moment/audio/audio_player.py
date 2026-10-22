"""本地 BGM 播放器（sounddevice + 双后端解码）。

关键约束：
- 音频回调里只做 numpy 切片与音量乘法：不解码、不写文件、不碰 GUI。
- 播放完成通过 ``raise sd.CallbackStop`` 结束流，回调内不做阻塞操作。
- 播放过程中换曲/停止都要能干净地释放 PortAudio stream。
- 设备不可用、文件缺失、格式不支持都必须抛明确异常，而不是静默失败。

解码交给 :mod:`rusty_moment.audio.decoder`（libsndfile + PyAV 双后端），
所以 AAC/M4A 也能放；``sd_module`` / ``decoder`` 可注入，便于无设备环境测试。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable

import numpy as np

from .decoder import ALL_SUPPORTED_SUFFIXES, DecodeError, DecodedAudio, decode_audio

log = logging.getLogger(__name__)

SUPPORTED_SUFFIXES = ALL_SUPPORTED_SUFFIXES
MAX_DECODED_BYTES = 400 * 1024 * 1024  # 由 decoder 内部检查，这里保留常量供外部引用


class AudioPlayerError(Exception):
    """播放器的通用错误。"""


class AudioFileError(AudioPlayerError):
    """文件不存在 / 无法解码 / 内存占用过大。"""


class AudioDeviceError(AudioPlayerError):
    """输出设备不可用。"""


class PlaybackState(str, Enum):
    IDLE = "idle"
    PLAYING = "playing"
    PAUSED = "paused"
    ERROR = "error"


@dataclass(frozen=True)
class AudioInfo:
    path: str
    duration_s: float
    samplerate: int
    channels: int
    format: str
    backend: str = ""


def _import_sounddevice() -> Any:
    try:
        import sounddevice as sd  # type: ignore

        return sd
    except Exception as exc:  # pragma: no cover - 无 PortAudio 环境
        raise AudioPlayerError(f"无法加载音频后端（PortAudio）：{exc}") from exc


class AudioPlayer:
    """单实例播放器：同一时刻只播放一个 BGM（重复播放会被拒绝）。"""

    def __init__(
        self,
        sd_module: Any | None = None,
        decoder: Callable[[str | Path], DecodedAudio] | None = None,
        blocksize: int = 1024,
    ) -> None:
        self._sd = sd_module if sd_module is not None else _import_sounddevice()
        self._decode = decoder or decode_audio
        self._blocksize = blocksize
        self._lock = threading.RLock()
        self._stream: Any = None
        self._data: np.ndarray | None = None
        self._info: AudioInfo | None = None
        self._pos = 0
        self._volume = 0.8
        self._loop = False
        self._state = PlaybackState.IDLE
        self._device_index: int | None = None
        self._samplerate = 48000
        self.on_finished: Callable[[], None] | None = None

    # -- 查询 --------------------------------------------------------------- #
    @property
    def state(self) -> PlaybackState:
        return self._state

    @property
    def is_playing(self) -> bool:
        return self._state in (PlaybackState.PLAYING, PlaybackState.PAUSED)

    @property
    def is_active(self) -> bool:
        """流是否仍在输出（暂停/结束都为 False）。"""
        stream = self._stream
        if stream is None:
            return False
        return bool(getattr(stream, "active", False))

    @property
    def info(self) -> AudioInfo | None:
        return self._info

    @property
    def volume(self) -> float:
        return self._volume

    @property
    def path(self) -> str:
        return self._info.path if self._info else ""

    @property
    def duration(self) -> float:
        return self._info.duration_s if self._info else 0.0

    @property
    def position(self) -> float:
        if self._data is None or self._samplerate <= 0:
            return 0.0
        return min(self._pos / self._samplerate, self.duration)

    @property
    def progress(self) -> float:
        total = self.duration
        return min(1.0, self.position / total) if total > 0 else 0.0

    # -- 设备 --------------------------------------------------------------- #
    def list_output_devices(self) -> list[tuple[int, str]]:
        devices = self._sd.query_devices()
        out: list[tuple[int, str]] = []
        for index, dev in enumerate(devices):
            if int(dev.get("max_output_channels", 0)) > 0:
                out.append((index, str(dev.get("name", f"设备 {index}"))))
        return out

    def resolve_output_device(self, spec: str | int | None) -> int | None:
        """把配置里的设备（索引或名称子串）解析成 sounddevice 设备索引。"""
        if spec is None or spec == "":
            return None
        if isinstance(spec, int):
            return spec
        text = str(spec).strip()
        if text.isdigit():
            index = int(text)
            devices = self._sd.query_devices()
            if 0 <= index < len(devices) and int(devices[index].get("max_output_channels", 0)) > 0:
                return index
            raise AudioDeviceError(f"输出设备索引 {index} 不可用")
        lowered = text.lower()
        matches: list[int] = []
        for index, dev in enumerate(self._sd.query_devices()):
            if int(dev.get("max_output_channels", 0)) <= 0:
                continue
            if lowered in str(dev.get("name", "")).lower():
                matches.append(index)
        if not matches:
            raise AudioDeviceError(f"找不到输出设备：{text}")
        return matches[0]

    # -- 载入 --------------------------------------------------------------- #
    def load(self, path: str | Path) -> AudioInfo:
        """解码到内存并返回音频信息；解码失败抛 :class:`AudioFileError`。"""
        file_path = Path(path)
        if not file_path.exists():
            raise AudioFileError(f"音乐文件不存在：{file_path}")
        if file_path.suffix.lower() not in SUPPORTED_SUFFIXES:
            raise AudioFileError(
                f"不支持的格式 {file_path.suffix}（当前支持：{', '.join(sorted(SUPPORTED_SUFFIXES))}）"
            )
        try:
            decoded = self._decode(file_path)
        except DecodeError as exc:
            raise AudioFileError(f"无法解码音频：{exc}") from exc
        except Exception as exc:
            raise AudioFileError(f"无法解码音频：{exc}") from exc
        data = np.ascontiguousarray(decoded.samples, dtype=np.float32)
        samplerate = int(decoded.samplerate)
        info = AudioInfo(
            path=str(file_path),
            duration_s=decoded.duration_s,
            samplerate=samplerate,
            channels=int(data.shape[1]),
            format=decoded.format,
            backend=decoded.backend,
        )
        with self._lock:
            self._data = data
            self._info = info
            self._samplerate = info.samplerate
            self._pos = 0
        return info

    # -- 播放控制 ----------------------------------------------------------- #
    def play(
        self,
        path: str | Path | None = None,
        *,
        volume: float | None = None,
        from_start: bool = True,
        loop: bool = False,
        device: str | int | None = None,
        samplerate_fallback: int | None = None,
    ) -> AudioInfo:
        """开始播放；已有播放时会先停止旧流（调用方应确保不会并发触发）。"""
        with self._lock:
            if path is not None:
                target = Path(path)
                if self._info is None or Path(self._info.path) != target:
                    self.load(target)
                elif from_start:
                    self._pos = 0
            if self._info is None:
                raise AudioFileError("没有已载入的音乐文件")
            if volume is not None:
                self._volume = max(0.0, min(1.0, float(volume)))
            if from_start:
                self._pos = 0
            self._loop = bool(loop)
            self._stop_stream_locked()
            device_index = self.resolve_output_device(device)
            self._device_index = device_index
            self._state = PlaybackState.PLAYING
            self._open_stream(device_index, samplerate_fallback)
            assert self._info is not None
            return self._info

    def _open_stream(self, device_index: int | None, samplerate_fallback: int | None) -> None:
        assert self._data is not None
        channels = int(self._data.shape[1])
        attempts: list[int] = [self._samplerate]
        if samplerate_fallback and samplerate_fallback not in attempts:
            attempts.append(int(samplerate_fallback))
        default_sr = None
        try:
            if device_index is not None:
                default_sr = int(self._sd.query_devices(device_index).get("default_samplerate", 0))
            else:
                default_sr = int(
                    self._sd.query_devices(kind="output").get("default_samplerate", 0)
                )
        except Exception:  # pragma: no cover - 某些驱动不实现查询
            default_sr = None
        if default_sr and default_sr not in attempts:
            attempts.append(default_sr)

        last_error: Exception | None = None
        for samplerate in attempts:
            data = self._resample_if_needed(samplerate)
            try:
                stream = self._sd.OutputStream(
                    samplerate=samplerate,
                    channels=channels,
                    dtype="float32",
                    blocksize=self._blocksize,
                    device=device_index,
                    callback=self._callback,
                    finished_callback=self._on_stream_finished,
                )
                stream.start()
            except Exception as exc:
                last_error = exc
                continue
            self._samplerate = samplerate
            self._data = data
            self._pos = 0
            self._stream = stream
            return
        self._state = PlaybackState.ERROR
        raise AudioDeviceError(f"无法打开音频输出设备：{last_error}")

    def _resample_if_needed(self, target_sr: int) -> np.ndarray:
        assert self._data is not None
        if target_sr == self._samplerate:
            return self._data
        # 线性插值足够：只是给音量骤降叠个 BGM，不需要高保真重采样
        src = self._data
        src_sr = self._samplerate
        new_len = int(round(src.shape[0] * target_sr / src_sr))
        if new_len <= 0:
            return src
        old_idx = np.linspace(0.0, 1.0, num=src.shape[0], endpoint=False)
        new_idx = np.linspace(0.0, 1.0, num=new_len, endpoint=False)
        out = np.empty((new_len, src.shape[1]), dtype=np.float32)
        for ch in range(src.shape[1]):
            out[:, ch] = np.interp(new_idx, old_idx, src[:, ch]).astype(np.float32)
        return out

    def _callback(self, outdata: Any, frames: int, time_info: Any, status: Any) -> None:
        """实时回调：只做切片与乘法。"""
        if status:
            log.debug("音频回调状态：%s", status)
        outdata.fill(0)
        data = self._data
        if data is None:
            return
        total = data.shape[0]
        pos = self._pos
        volume = self._volume
        if pos >= total:
            if self._loop:
                pos = 0
            else:
                raise self._sd.CallbackStop
        remaining = total - pos
        take = min(frames, remaining)
        if take > 0:
            chunk = data[pos : pos + take]
            outdata[:take] = chunk if volume == 1.0 else chunk * volume
        self._pos = pos + take

    def _on_stream_finished(self) -> None:
        with self._lock:
            if self._state == PlaybackState.PLAYING:
                self._state = PlaybackState.IDLE
        callback = self.on_finished
        if callback is not None:
            try:
                callback()
            except Exception:  # pragma: no cover
                log.exception("播放结束回调异常")

    def pause(self) -> None:
        with self._lock:
            stream = self._stream
            if stream is not None and self._state == PlaybackState.PLAYING:
                try:
                    stream.stop()
                except Exception as exc:  # pragma: no cover
                    raise AudioPlayerError(f"暂停失败：{exc}") from exc
                self._state = PlaybackState.PAUSED

    def resume(self) -> None:
        with self._lock:
            stream = self._stream
            if stream is not None and self._state == PlaybackState.PAUSED:
                try:
                    stream.start()
                except Exception as exc:  # pragma: no cover
                    raise AudioPlayerError(f"恢复播放失败：{exc}") from exc
                self._state = PlaybackState.PLAYING

    def stop(self) -> None:
        with self._lock:
            self._stop_stream_locked()
            self._pos = 0
            self._state = PlaybackState.IDLE

    def _stop_stream_locked(self) -> None:
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            stream.stop()
        except Exception as exc:  # pragma: no cover
            log.warning("停止音频流时出错：%s", exc)
        try:
            stream.close()
        except Exception:  # pragma: no cover
            pass

    def set_volume(self, volume: float) -> None:
        self._volume = max(0.0, min(1.0, float(volume)))

    def prepare(self, path: str | Path) -> AudioInfo | None:
        """后台预解码（用于切曲后预热）。正在播放时直接跳过，避免打断当前音乐。"""
        with self._lock:
            if self.is_active:
                return None
            return self.load(path)

    def is_prepared(self, path: str | Path) -> bool:
        target = Path(path)
        return bool(self._info is not None and Path(self._info.path) == target)

    def wait_until_inactive(self, timeout: float = 600.0, poll: float = 0.02) -> bool:
        """等待播放流结束（供事件线程使用），返回是否正常结束。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.is_active:
                return True
            time.sleep(poll)
        return False

    def close(self) -> None:
        self.stop()
        with self._lock:
            self._data = None
            self._info = None
            self._state = PlaybackState.IDLE

"""音频解码：优先 libsndfile（soundfile），失败则用 PyAV（自带 FFmpeg）。

为什么要双后端：
- libsndfile 对 WAV/MP3/FLAC/OGG 又快又稳，但不支持 AAC/M4A——而用户音乐库里
  最常见的恰恰是 `.aac` / `.m4a`（本机三首默认曲子全是 aac）。
- PyAV 随 faster-whisper 一起装好了，自带 FFmpeg 库，不需要用户另装 ffmpeg.exe。

统一输出：float32、形状 ``(frames, channels)``、声道数 <= 2。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

log = logging.getLogger(__name__)

# libsndfile 能稳定处理的容器/编码
SOUNDFILE_SUFFIXES = {".wav", ".mp3", ".flac", ".ogg", ".oga", ".opus", ".aiff", ".aif"}
# 交给 PyAV 的格式（FFmpeg 全家桶）
AV_SUFFIXES = {".aac", ".m4a", ".mp4", ".wma", ".wmv", ".ape", ".alac", ".webm", ".mkv", ".mp2"}
ALL_SUPPORTED_SUFFIXES = SOUNDFILE_SUFFIXES | AV_SUFFIXES

MAX_DECODED_BYTES = 400 * 1024 * 1024


class DecodeError(Exception):
    """所有后端都解不开这个文件。"""


@dataclass(frozen=True)
class DecodedAudio:
    samples: np.ndarray  # (frames, channels) float32
    samplerate: int
    backend: str  # "soundfile" / "pyav"
    format: str  # 展示用：WAV / MP3 / AAC ...

    @property
    def duration_s(self) -> float:
        return self.samples.shape[0] / float(self.samplerate) if self.samplerate else 0.0

    @property
    def channels(self) -> int:
        return int(self.samples.shape[1])


def _post_process(samples: np.ndarray, samplerate: int) -> np.ndarray:
    data = np.ascontiguousarray(samples, dtype=np.float32)
    if data.ndim == 1:
        data = data.reshape(-1, 1)
    if data.shape[1] > 2:
        data = np.ascontiguousarray(data[:, :2])
    if data.shape[0] == 0:
        raise DecodeError("解码后没有采样数据")
    if data.nbytes > MAX_DECODED_BYTES:
        raise DecodeError(
            f"解码后占用 {data.nbytes / 1024 / 1024:.0f}MB，过大；请换更短的文件或先转码"
        )
    if samplerate <= 0:
        raise DecodeError(f"采样率异常：{samplerate}")
    return data


def decode_with_soundfile(path: Path) -> DecodedAudio:
    import soundfile as sf

    info = sf.info(str(path))
    data, samplerate = sf.read(str(path), dtype="float32", always_2d=True)
    samples = _post_process(data, int(samplerate))
    return DecodedAudio(
        samples=samples,
        samplerate=int(samplerate),
        backend="soundfile",
        format=str(info.format or path.suffix.lstrip(".")).upper(),
    )


def decode_with_pyav(path: Path, target_samplerate: int | None = None) -> DecodedAudio:
    try:
        import av  # PyAV
    except Exception as exc:  # pragma: no cover - 依赖缺失
        raise DecodeError(f"未安装 PyAV，无法解码 {path.suffix}：{exc}") from exc

    chunks: list[np.ndarray] = []
    samplerate = int(target_samplerate or 0)
    container_format = ""
    try:
        with av.open(str(path)) as container:
            container_format = str(container.format.name or "").split(",")[0].upper()
            if not container.streams.audio:
                raise DecodeError("文件里没有音频轨道")
            stream = container.streams.audio[0]
            if not samplerate:
                samplerate = int(stream.rate or 44100)
            resampler = av.AudioResampler(format="fltp", layout="stereo", rate=samplerate)
            for frame in container.decode(stream):
                for out in resampler.resample(frame):
                    chunks.append(np.asarray(out.to_ndarray(), dtype=np.float32).T)
            for out in resampler.resample(None):  # flush
                chunks.append(np.asarray(out.to_ndarray(), dtype=np.float32).T)
    except DecodeError:
        raise
    except Exception as exc:
        raise DecodeError(f"PyAV 解码失败：{exc}") from exc

    if not chunks:
        raise DecodeError("PyAV 没有解出任何音频帧")
    samples = np.concatenate(chunks, axis=0)
    data = _post_process(samples, samplerate)
    suffix_label = path.suffix.lstrip(".").upper()
    return DecodedAudio(
        samples=data,
        samplerate=samplerate,
        backend="pyav",
        format=suffix_label or container_format or "AUDIO",
    )


def decode_audio(path: str | Path, *, target_samplerate: int | None = None) -> DecodedAudio:
    """解码任意受支持音频；两个后端都失败时抛 :class:`DecodeError`。"""
    file_path = Path(path)
    if not file_path.exists():
        raise DecodeError(f"音乐文件不存在：{file_path}")
    suffix = file_path.suffix.lower()
    errors: list[str] = []

    order: list[Any]
    if suffix in SOUNDFILE_SUFFIXES or suffix not in ALL_SUPPORTED_SUFFIXES:
        order = [decode_with_soundfile, decode_with_pyav]
    else:
        order = [decode_with_pyav, decode_with_soundfile]

    for decoder in order:
        try:
            result = (
                decoder(file_path)
                if decoder is decode_with_soundfile
                else decoder(file_path, target_samplerate)
            )
        except DecodeError as exc:
            errors.append(str(exc))
            continue
        except Exception as exc:  # pragma: no cover - 防御
            errors.append(f"{decoder.__name__}: {exc}")
            continue
        log.debug(
            "解码 %s 使用 %s（%s, %d Hz, %.1fs）",
            file_path.name,
            result.backend,
            result.format,
            result.samplerate,
            result.duration_s,
        )
        return result

    raise DecodeError(
        f"无法解码 {file_path.name}（{'；'.join(errors) or '后端不可用'}）"
    )


def probe_duration(path: str | Path) -> float:
    """只取时长（解码整首太慢时用）。"""
    try:
        return decode_audio(path).duration_s
    except DecodeError:
        return 0.0

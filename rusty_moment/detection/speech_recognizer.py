"""语音转文字：可替换接口 + 本地 faster-whisper 实现。

隐私相关的硬约束（都在这里落地）：
- 只接受内存里的 PCM，默认不落盘；
- 不保存完整转录文本（调用方拿到 Transcript 用完就丢，日志只记元数据）；
- 失败/超时抛 :class:`RecognizerError`，由上层回退到「没有文本」的模式，
  而不是编造文本。

模型首次使用会从 Hugging Face 下载到本机（可在设置里指定目录）。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np

log = logging.getLogger(__name__)

TARGET_SAMPLE_RATE = 16000


class RecognizerError(Exception):
    """识别失败（模型缺失、解码异常、超时）。"""


class RecognizerUnavailable(RecognizerError):
    """识别功能未启用/依赖缺失。"""


@dataclass
class Transcript:
    text: str
    language: str = ""
    duration_s: float = 0.0
    latency_ms: float = 0.0
    average_logprob: float | None = None

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()


class SpeechRecognizer(Protocol):
    name: str

    @property
    def available(self) -> bool: ...

    def warmup(self) -> None: ...

    def transcribe(self, pcm: np.ndarray, samplerate: int) -> Transcript: ...


def resample_linear(pcm: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    if src_rate == dst_rate or pcm.size == 0:
        return pcm.astype(np.float32, copy=False)
    duration = pcm.shape[0] / float(src_rate)
    target_len = max(1, int(round(duration * dst_rate)))
    src_idx = np.linspace(0.0, 1.0, num=pcm.shape[0], endpoint=False)
    dst_idx = np.linspace(0.0, 1.0, num=target_len, endpoint=False)
    return np.interp(dst_idx, src_idx, pcm).astype(np.float32)


class NullRecognizer:
    """未启用语音识别时的实现：明确不可用。"""

    name = "none"

    @property
    def available(self) -> bool:
        return False

    def warmup(self) -> None:
        return None

    def transcribe(self, pcm: np.ndarray, samplerate: int) -> Transcript:
        raise RecognizerUnavailable("未启用语音识别（阶段 2 可在设置中开启 faster-whisper）")


class FasterWhisperRecognizer:
    """本地 faster-whisper（CTranslate2 后端）。"""

    name = "faster_whisper"

    def __init__(
        self,
        model_size: str = "small",
        *,
        device: str = "cpu",
        compute_type: str = "int8",
        language: str = "zh",
        download_root: str | Path | None = None,
        beam_size: int = 1,
        initial_prompt: str = "以下是普通话的句子。",
        model: Any = None,
    ) -> None:
        self.model_size = model_size or "small"
        self.device = device
        self.compute_type = compute_type
        self.language = language or "zh"
        self.download_root = str(download_root) if download_root else None
        self.beam_size = max(1, int(beam_size))
        # Whisper 对中文常输出繁体；initial_prompt 能显著提高简体输出比例
        self.initial_prompt = initial_prompt
        self._model = model
        self._load_error = ""

    # -- 生命周期 ----------------------------------------------------------- #
    @property
    def available(self) -> bool:
        return self._model is not None

    @property
    def load_error(self) -> str:
        return self._load_error

    def warmup(self) -> None:
        if self._model is not None:
            return
        try:
            from faster_whisper import WhisperModel  # 延迟导入：没装也能启动程序
        except Exception as exc:  # pragma: no cover - 依赖缺失
            self._load_error = f"未安装 faster-whisper：{exc}"
            raise RecognizerUnavailable(self._load_error) from exc
        t0 = time.monotonic()
        try:
            self._model = WhisperModel(
                self.model_size,
                device=self.device,
                compute_type=self.compute_type,
                download_root=self.download_root,
            )
        except Exception as exc:
            self._load_error = f"加载语音模型失败：{exc}"
            raise RecognizerError(self._load_error) from exc
        log.info(
            "语音模型 %s 加载完成（%.1fs，device=%s, compute=%s）",
            self.model_size,
            time.monotonic() - t0,
            self.device,
            self.compute_type,
        )

    # -- 识别 --------------------------------------------------------------- #
    def transcribe(self, pcm: np.ndarray, samplerate: int) -> Transcript:
        if self._model is None:
            self.warmup()
        audio = resample_linear(np.asarray(pcm, dtype=np.float32).reshape(-1), samplerate, TARGET_SAMPLE_RATE)
        t0 = time.monotonic()
        try:
            segments, info = self._model.transcribe(
                audio,
                language=None if self.language in ("", "auto") else self.language,
                beam_size=self.beam_size,
                vad_filter=False,  # 我们自己已经做过 VAD
                condition_on_previous_text=False,
                without_timestamps=True,
                initial_prompt=self.initial_prompt or None,
            )
            texts = [segment.text.strip() for segment in segments if segment.text.strip()]
        except Exception as exc:
            raise RecognizerError(f"语音识别失败：{exc}") from exc
        text = "".join(texts).strip()
        latency_ms = (time.monotonic() - t0) * 1000.0
        return Transcript(
            text=text,
            language=str(getattr(info, "language", "") or self.language),
            duration_s=audio.shape[0] / float(TARGET_SAMPLE_RATE),
            latency_ms=latency_ms,
        )


def build_recognizer(config: Any, model_dir: str | Path | None = None) -> SpeechRecognizer:
    """根据配置构造识别器；未启用返回 :class:`NullRecognizer`。"""
    backend = getattr(config, "backend", "none")
    if not getattr(config, "enabled", False) or backend == "none":
        return NullRecognizer()
    if backend == "faster_whisper":
        return FasterWhisperRecognizer(
            model_size=getattr(config, "model_size", "small"),
            language=getattr(config, "language", "zh"),
            download_root=model_dir,
            compute_type=getattr(config, "compute_type", "int8"),
            initial_prompt=getattr(config, "initial_prompt", "以下是普通话的句子。"),
        )
    return NullRecognizer()

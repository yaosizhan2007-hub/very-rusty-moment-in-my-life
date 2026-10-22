"""轻量 VAD + 语音段切分（纯 numpy，不依赖 webrtcvad）。

为什么自己写：
- 少一个需要编译/native 依赖的库，打包更稳；
- 能量 + 自适应噪声底足以切出「一句游戏语音」这种场景；
- 可以注入假数据在单元测试里精确验证边界（语音起止、最短语音、最长会话）。

特点：
- 帧级能量（dBFS）+ 自适应噪声底（低频分位数），在风扇/键盘噪声下不会一直判语音；
- 只有在「语音持续够久」后才确认一段 utterance，避免咳嗽/敲键盘触发；
- 段内长静音可分句，但短停顿保留在同一句里（悲壮台词常常是断断续续的）。
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from typing import Deque

import numpy as np

log = logging.getLogger(__name__)

EPS = 1e-10


def rms_to_db(rms: float) -> float:
    return 20.0 * float(np.log10(max(rms, EPS)))


def frame_db(frame: np.ndarray) -> float:
    if frame.size == 0:
        return -120.0
    return rms_to_db(float(np.sqrt(np.mean(np.square(frame, dtype=np.float64)))))


@dataclass
class VadConfig:
    frame_ms: int = 30
    """能量阈值：高于「噪声底 + margin」且高于 absolute_floor 才算语音。"""

    absolute_floor_db: float = -55.0
    margin_db: float = 8.0
    adaptive: bool = True
    noise_window_frames: int = 200
    noise_percentile: float = 0.2
    min_speech_ms: int = 400
    """低于这个时长的「语音」当噪声丢掉。"""
    min_silence_ms: int = 500
    """段内静音超过这个长度就认为一句话结束。"""
    max_utterance_ms: int = 15000
    """单句上限，防止一直说话把缓冲撑爆。"""
    pre_roll_ms: int = 200
    """开始判定时向前多留一点，避免吃掉字头。"""
    trim_tail_ms: int = 150


@dataclass
class UtteranceFeatures:
    """一句话的声学特征，供规则层打分（不含任何文本）。"""

    duration_s: float
    voiced_ratio: float
    mean_db: float
    peak_db: float
    tail_db: float
    volume_drop_ratio: float
    longest_pause_s: float
    volume_spike_db: float

    def to_dict(self) -> dict[str, float]:
        return {
            "duration_s": self.duration_s,
            "voiced_ratio": self.voiced_ratio,
            "mean_db": self.mean_db,
            "peak_db": self.peak_db,
            "tail_db": self.tail_db,
            "volume_drop_ratio": self.volume_drop_ratio,
            "longest_pause_s": self.longest_pause_s,
            "volume_spike_db": self.volume_spike_db,
        }


@dataclass
class Utterance:
    pcm: np.ndarray
    samplerate: int
    features: UtteranceFeatures
    started_at: float = 0.0
    ended_at: float = 0.0

    @property
    def duration_s(self) -> float:
        return self.features.duration_s


def compute_features(
    pcm: np.ndarray, samplerate: int, frame_ms: int = 30, noise_floor_db: float = -80.0
) -> UtteranceFeatures:
    """从一段音频算特征（独立函数，便于直接测试）。"""
    frame_len = max(1, int(samplerate * frame_ms / 1000))
    frame_count = max(1, len(pcm) // frame_len)
    frames = [
        pcm[i * frame_len : (i + 1) * frame_len] for i in range(frame_count)
    ]
    dbs = np.array([frame_db(f) for f in frames], dtype=np.float64)
    duration = frame_count * frame_len / float(samplerate)
    threshold = noise_floor_db + 8.0
    voiced = dbs > threshold
    voiced_ratio = float(np.mean(voiced)) if len(dbs) else 0.0

    peak = float(np.max(dbs)) if len(dbs) else -120.0
    mean = float(np.mean(dbs[voiced])) if voiced.any() else float(np.mean(dbs) if len(dbs) else -120.0)
    tail_frames = max(1, int(round(0.25 * 1000 / frame_ms)))
    tail = float(np.mean(dbs[-tail_frames:])) if len(dbs) else -120.0
    drop_ratio = 0.0 if peak <= -100 else max(0.0, min(1.0, 1.0 - (tail - noise_floor_db) / max(peak - noise_floor_db, 1.0)))

    longest_silence = 0
    current = 0
    for is_voiced in voiced:
        if is_voiced:
            current = 0
        else:
            current += 1
            longest_silence = max(longest_silence, current)
    longest_pause_s = longest_silence * frame_len / float(samplerate)

    # 突然提高的音量：峰值比语音段平均高多少 dB
    spike = max(0.0, peak - mean)

    return UtteranceFeatures(
        duration_s=duration,
        voiced_ratio=voiced_ratio,
        mean_db=mean,
        peak_db=peak,
        tail_db=tail,
        volume_drop_ratio=drop_ratio,
        longest_pause_s=longest_pause_s,
        volume_spike_db=spike,
    )


class VoiceSegmenter:
    """流式切句：push 音频块，返回已完成的 utterance 列表。"""

    def __init__(self, samplerate: int, config: VadConfig | None = None) -> None:
        self.samplerate = int(samplerate)
        self.config = config or VadConfig()
        self._frame_len = max(1, int(self.samplerate * self.config.frame_ms / 1000))
        self._buffer = np.zeros(0, dtype=np.float32)
        self._pre_roll: Deque[np.ndarray] = deque(
            maxlen=max(1, int(self.config.pre_roll_ms / self.config.frame_ms) + 1)
        )
        self._noise_history: Deque[float] = deque(maxlen=self.config.noise_window_frames)
        self._noise_floor = -70.0
        # 启动学习期：先用约 0.5 秒只收噪声样本，避免一开机把环境噪声当台词
        self._warmup_left = max(1, int(500 / max(1, self.config.frame_ms)))
        self._in_speech = False
        self._frames: list[np.ndarray] = []
        self._frames_db: list[float] = []
        self._silence_frames = 0
        self._speech_frames = 0
        self._pending_lead: list[np.ndarray] = []

    # -- 配置 --------------------------------------------------------------- #
    @property
    def noise_floor_db(self) -> float:
        return self._noise_floor

    def threshold_db(self) -> float:
        return max(self.config.absolute_floor_db, self._noise_floor + self.config.margin_db)

    def reset(self) -> None:
        self._buffer = np.zeros(0, dtype=np.float32)
        self._pre_roll.clear()
        self._frames.clear()
        self._frames_db.clear()
        self._silence_frames = 0
        self._speech_frames = 0
        self._in_speech = False
        self._pending_lead.clear()
        self._warmup_left = max(1, int(500 / max(1, self.config.frame_ms)))

    # -- 输入 --------------------------------------------------------------- #
    def push(self, block: np.ndarray) -> list[Utterance]:
        if block.ndim > 1:
            block = block.mean(axis=1)
        data = np.concatenate([self._buffer, block.astype(np.float32, copy=False)])
        out: list[Utterance] = []
        frame_len = self._frame_len
        offset = 0
        while len(data) - offset >= frame_len:
            frame = data[offset : offset + frame_len]
            offset += frame_len
            self._process_frame(frame, out)
        self._buffer = data[offset:]
        return out

    def flush(self) -> list[Utterance]:
        """停止监听时把未完成的段收尾。"""
        out: list[Utterance] = []
        if self._in_speech and self._frames:
            utterance = self._finalize(force=True)
            if utterance is not None:
                out.append(utterance)
        self.reset()
        return out

    # -- 内部 --------------------------------------------------------------- #
    def _process_frame(self, frame: np.ndarray, out: list[Utterance]) -> None:
        db = frame_db(frame)
        cfg = self.config
        if cfg.adaptive and (not self._in_speech or self._warmup_left > 0):
            self._noise_history.append(db)
            if len(self._noise_history) >= 10:
                self._noise_floor = float(
                    np.percentile(np.array(self._noise_history), cfg.noise_percentile * 100)
                )

        if self._warmup_left > 0:
            self._warmup_left -= 1
            # 学习期只吸收噪声样本，除非响度明显高于当前噪声底
            if db < self._noise_floor + 15.0:
                return

        threshold = self.threshold_db()
        is_voiced = db > threshold

        if not self._in_speech:
            if is_voiced:
                self._in_speech = True
                self._silence_frames = 0
                self._speech_frames = 1
                self._frames = list(self._pending_lead) + [frame]
                self._frames_db = [frame_db(f) for f in self._pending_lead] + [db]
                self._pending_lead.clear()
                self._pre_roll.clear()
            else:
                self._pending_lead.append(frame)
                keep = max(1, int(cfg.pre_roll_ms / cfg.frame_ms))
                if len(self._pending_lead) > keep:
                    self._pending_lead.pop(0)
            return

        # 正在语音
        self._frames.append(frame)
        self._frames_db.append(db)
        if is_voiced:
            self._silence_frames = 0
            self._speech_frames += 1
        else:
            self._silence_frames += 1

        total_frames = len(self._frames)
        total_ms = total_frames * cfg.frame_ms
        silence_ms = self._silence_frames * cfg.frame_ms

        if silence_ms >= cfg.min_silence_ms:
            utterance = self._finalize()
            if utterance is not None:
                out.append(utterance)
            self._in_speech = False
            self._frames = []
            self._frames_db = []
            self._speech_frames = 0
            self._silence_frames = 0
        elif total_ms >= cfg.max_utterance_ms:
            utterance = self._finalize(force=True)
            if utterance is not None:
                out.append(utterance)
            self._in_speech = False
            self._frames = []
            self._frames_db = []
            self._speech_frames = 0
            self._silence_frames = 0

    def _finalize(self, force: bool = False) -> Utterance | None:
        cfg = self.config
        frames = self._frames
        if not frames:
            return None
        speech_ms = self._speech_frames * cfg.frame_ms
        if speech_ms < cfg.min_speech_ms and not force:
            log.debug("丢弃过短语音：%dms", speech_ms)
            return None
        # 去掉尾部静音（只保留 trim_tail_ms 的缓冲），否则特征里的音量衰减会被静音污染
        keep_tail = int(cfg.trim_tail_ms / cfg.frame_ms)
        tail_cut = max(0, self._silence_frames - keep_tail)
        if tail_cut:
            frames = frames[:-tail_cut] if tail_cut < len(frames) else frames[-1:]
        pcm = np.concatenate(frames).astype(np.float32, copy=False)
        if pcm.size == 0:
            return None
        features = compute_features(
            pcm, self.samplerate, cfg.frame_ms, noise_floor_db=self._noise_floor
        )
        return Utterance(pcm=pcm, samplerate=self.samplerate, features=features)


def segment_offline(
    pcm: np.ndarray, samplerate: int, config: VadConfig | None = None, block: int = 480
) -> list[Utterance]:
    """离线切句（测试/调试用）：整段音频一次算完。"""
    segmenter = VoiceSegmenter(samplerate, config)
    out: list[Utterance] = []
    for start in range(0, len(pcm), block):
        out.extend(segmenter.push(pcm[start : start + block]))
    out.extend(segmenter.flush())
    return out


def synth_speech_like(
    duration_s: float,
    samplerate: int,
    *,
    amplitude: float = 0.2,
    tail_amplitude: float | None = None,
    seed: int = 0,
) -> np.ndarray:
    """生成「像说话」的测试音频：带抖动幅度的噪声，避免依赖真实录音。"""
    rng = np.random.default_rng(seed)
    n = int(duration_s * samplerate)
    noise = rng.standard_normal(n).astype(np.float32)
    envelope = np.ones(n, dtype=np.float32) * amplitude
    if tail_amplitude is not None:
        fade = max(1, n // 3)
        envelope[-fade:] = np.linspace(amplitude, tail_amplitude, fade, dtype=np.float32)
    # 加点 1.5kHz 的类语音调制，让频谱不那么像白噪
    t = np.arange(n) / samplerate
    tone = 0.3 * np.sin(2 * np.pi * 1500 * t).astype(np.float32)
    return ((noise * 0.7 + tone) * envelope).astype(np.float32)

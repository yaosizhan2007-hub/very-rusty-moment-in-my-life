"""VAD 切句与声学特征测试（全部用合成音频，不依赖真实录音）。"""

from __future__ import annotations

import numpy as np
import pytest

from rusty_moment.detection.vad import (
    VadConfig,
    VoiceSegmenter,
    compute_features,
    frame_db,
    segment_offline,
    synth_speech_like,
)

SR = 16000


def silence(seconds: float, sr: int = SR) -> np.ndarray:
    return np.zeros(int(seconds * sr), dtype=np.float32)


def test_frame_db_levels():
    quiet = np.zeros(480, dtype=np.float32)
    loud = np.ones(480, dtype=np.float32) * 0.5
    assert frame_db(quiet) < -100
    assert -10 < frame_db(loud) < -5


def test_segments_single_utterance_between_silences():
    audio = np.concatenate(
        [silence(0.6), synth_speech_like(1.0, SR, amplitude=0.2), silence(0.8)]
    )
    utterances = segment_offline(audio, SR)

    assert len(utterances) == 1
    utt = utterances[0]
    assert 0.7 <= utt.duration_s <= 1.4
    assert utt.pcm.size > 0
    assert utt.features.voiced_ratio > 0.5
    assert utt.features.peak_db > -30


def test_short_noise_is_dropped():
    audio = np.concatenate([silence(0.5), synth_speech_like(0.1, SR), silence(0.8)])
    config = VadConfig(min_speech_ms=400)
    assert segment_offline(audio, SR, config) == []


def test_long_session_is_cut_by_max_utterance():
    audio = np.concatenate([silence(0.4), synth_speech_like(2.0, SR), silence(0.8)])
    config = VadConfig(max_utterance_ms=1000)
    utterances = segment_offline(audio, SR, config)
    assert len(utterances) >= 2
    assert all(utt.duration_s <= 1.4 for utt in utterances)


def test_flush_returns_pending_utterance():
    segmenter = VoiceSegmenter(SR, VadConfig(min_silence_ms=2000))
    segmenter.push(synth_speech_like(0.8, SR))
    assert segmenter.flush(), "停止监听时未收尾的半句应当被输出"


def test_noise_floor_adapts_and_threshold_rises():
    segmenter = VoiceSegmenter(SR, VadConfig(adaptive=True))
    # 先喂一段稳定的中等噪声，让噪声底抬起来
    noise = (np.random.default_rng(1).standard_normal(SR) * 0.01).astype(np.float32)
    segmenter.push(noise)
    assert segmenter.noise_floor_db > -50
    assert segmenter.threshold_db() > segmenter.noise_floor_db


def test_features_detect_volume_drop():
    steady = synth_speech_like(1.0, SR, amplitude=0.3)
    fading = synth_speech_like(1.0, SR, amplitude=0.3, tail_amplitude=0.02)
    steady_features = compute_features(steady, SR)
    fading_features = compute_features(fading, SR)
    assert fading_features.volume_drop_ratio > steady_features.volume_drop_ratio
    assert fading_features.tail_db < steady_features.tail_db


def test_features_count_internal_pause():
    speech = synth_speech_like(0.5, SR, amplitude=0.2)
    audio = np.concatenate([speech, silence(0.7), speech])
    features = compute_features(audio, SR)
    assert features.longest_pause_s >= 0.5


def test_stereo_block_is_mixed_to_mono():
    segmenter = VoiceSegmenter(SR)
    stereo = np.stack([synth_speech_like(0.6, SR)] * 2, axis=1)
    segmenter.push(stereo)
    out = segmenter.flush()
    assert out and out[0].pcm.ndim == 1


def test_reset_clears_state():
    segmenter = VoiceSegmenter(SR)
    segmenter.push(synth_speech_like(0.5, SR))
    segmenter.reset()
    assert segmenter.push(silence(0.1)) == []


@pytest.mark.parametrize("block_ms", [10, 30, 60])
def test_block_size_does_not_change_result(block_ms):
    audio = np.concatenate([silence(0.5), synth_speech_like(1.0, SR), silence(0.8)])
    block = int(SR * block_ms / 1000)
    utterances = segment_offline(audio, SR, block=block)
    assert len(utterances) == 1
    assert 0.7 <= utterances[0].duration_s <= 1.5

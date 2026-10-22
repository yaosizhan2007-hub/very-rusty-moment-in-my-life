"""语音识别层测试：重采样、结构化校验、配置映射、模型调用（用假模型）。"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from rusty_moment.config.config_manager import STTConfig
from rusty_moment.detection.emotion_analyzer import (
    AnalysisError,
    AnalysisUnavailable,
    NullAnalyzer,
    parse_scores,
)
from rusty_moment.detection.speech_recognizer import (
    FasterWhisperRecognizer,
    NullRecognizer,
    RecognizerUnavailable,
    build_recognizer,
    resample_linear,
)


def test_resample_changes_length_and_keeps_signal():
    pcm = np.sin(np.linspace(0, 100, 44100)).astype(np.float32)
    resampled = resample_linear(pcm, 44100, 16000)
    assert resampled.shape[0] == pytest.approx(16000, abs=2)
    assert resampled.dtype == np.float32


def test_resample_same_rate_is_noop():
    pcm = np.ones(100, dtype=np.float32)
    assert resample_linear(pcm, 16000, 16000).shape == (100,)


def test_null_recognizer_is_explicitly_unavailable():
    recognizer = NullRecognizer()
    assert recognizer.available is False
    with pytest.raises(RecognizerUnavailable):
        recognizer.transcribe(np.zeros(1600, dtype=np.float32), 16000)


def test_build_recognizer_respects_config():
    assert isinstance(build_recognizer(STTConfig(enabled=False)), NullRecognizer)
    assert isinstance(build_recognizer(STTConfig(enabled=True, backend="none")), NullRecognizer)
    built = build_recognizer(
        STTConfig(enabled=True, backend="faster_whisper", model_size="base", language="zh")
    )
    assert isinstance(built, FasterWhisperRecognizer)
    assert built.model_size == "base"
    assert built.available is False  # 还没 warmup


class FakeModel:
    def __init__(self, segments: list[str]) -> None:
        self.segments = segments
        self.calls: list[dict] = []

    def transcribe(self, audio, **kwargs):
        self.calls.append({"audio": audio, **kwargs})
        segments = [SimpleNamespace(text=text) for text in self.segments]
        return iter(segments), SimpleNamespace(language="zh")


def test_transcribe_joins_segments_and_strips():
    model = FakeModel([" 别管我", "你们快走 "])
    recognizer = FasterWhisperRecognizer(model=model, language="zh")
    transcript = recognizer.transcribe(np.zeros(16000, dtype=np.float32), 16000)
    assert transcript.text == "别管我你们快走"
    assert transcript.language == "zh"
    assert transcript.latency_ms >= 0
    assert model.calls[0]["vad_filter"] is False, "已有外部 VAD，不应重复开"


def test_transcribe_failure_raises_recognizer_error():
    class BoomModel:
        def transcribe(self, audio, **kwargs):
            raise RuntimeError("ctranslate2 exploded")

    recognizer = FasterWhisperRecognizer(model=BoomModel())
    with pytest.raises(Exception) as excinfo:
        recognizer.transcribe(np.zeros(1600, dtype=np.float32), 16000)
    assert "语音识别失败" in str(excinfo.value)


def test_parse_scores_clamps_and_truncates_reason():
    scores = parse_scores(
        {
            "dramatic_score": 2.5,
            "context_score": -1,
            "comedic_potential": 0.4,
            "confidence": 0.8,
            "reason": "x" * 500,
        }
    )
    # 有任何一个 > 1 → 视为 10 分制，整体除以 10
    assert scores.dramatic_score == pytest.approx(0.25)
    assert scores.context_score == 0.0
    assert len(scores.reason) <= 200
    assert 0.0 <= scores.score <= 1.0


def test_parse_scores_normalizes_ten_point_scale():
    """真机遇到过的输出：1 / 3 / 2（10 分制整数）。"""
    scores = parse_scores(
        {
            "dramatic_score": 1,
            "context_score": 3,
            "comedic_potential": 2,
            "confidence": 0.7,
            "reason": "普通聊天",
        }
    )
    assert scores.dramatic_score == pytest.approx(0.1)
    assert scores.context_score == pytest.approx(0.3)
    assert scores.comedic_potential == pytest.approx(0.2)
    assert scores.confidence == pytest.approx(0.07)


def test_parse_scores_keeps_zero_one_scale():
    scores = parse_scores(
        {
            "dramatic_score": 0.9,
            "context_score": 0.8,
            "comedic_potential": 0.4,
            "confidence": 0.85,
        }
    )
    assert scores.dramatic_score == pytest.approx(0.9)
    assert scores.confidence == pytest.approx(0.85)


def test_parse_scores_rejects_missing_fields():
    with pytest.raises(AnalysisError):
        parse_scores({"dramatic_score": 0.5})
    with pytest.raises(AnalysisError):
        parse_scores("not a dict")  # type: ignore[arg-type]


def test_null_analyzer_is_unavailable():
    analyzer = NullAnalyzer()
    assert analyzer.available is False
    with pytest.raises(AnalysisUnavailable):
        analyzer.analyze("别管我")


def test_emotion_score_uses_confidence():
    base = dict(dramatic_score=1.0, context_score=1.0, comedic_potential=0.0, reason="")
    high = parse_scores({**base, "confidence": 1.0})
    low = parse_scores({**base, "confidence": 0.2})
    assert high.score > low.score

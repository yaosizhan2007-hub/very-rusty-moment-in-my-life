"""检测流水线集成测试：合成音频 → VAD → 识别 → 打分 → 触发回调。

用替身麦克风与替身识别器，不依赖真实声卡与模型，但在真实线程里跑完整链路。
"""

from __future__ import annotations

import time

import numpy as np
import pytest

from fakes import FakeCapture, FakeRecognizer
from rusty_moment.config.config_manager import AppConfig
from rusty_moment.core.state_machine import EventState, StateMachine
from rusty_moment.detection.detection_pipeline import DetectionPipeline, make_vad_config
from rusty_moment.detection.keyword_detector import KeywordDetector
from rusty_moment.detection.rule_analyzer import RuleAnalyzer
from rusty_moment.detection.speech_recognizer import RecognizerError
from rusty_moment.detection.trigger_engine import TriggerEngine
from rusty_moment.detection.vad import synth_speech_like

SR = 16000
BLOCK = 480  # 30ms


def silence(seconds: float) -> np.ndarray:
    return np.zeros(int(seconds * SR), dtype=np.float32)


def speech(seconds: float) -> np.ndarray:
    return synth_speech_like(seconds, SR, amplitude=0.25)


def blocks_for(audio: np.ndarray) -> list[np.ndarray]:
    return [audio[i : i + BLOCK] for i in range(0, len(audio), BLOCK)]


def build_pipeline(
    *,
    text: str = "别管我，你们快走",
    recognizer_error: Exception | None = None,
    config: AppConfig | None = None,
    state_machine: StateMachine | None = None,
    stt_enabled: bool = True,
    ai_analyzer=None,
    audio: np.ndarray | None = None,
):
    cfg = config or AppConfig()
    cfg.mic.sample_rate = SR
    if stt_enabled:
        cfg.stt.enabled = True
        cfg.stt.backend = "faster_whisper"
    cfg.trigger.min_voice_seconds = 0.5
    cfg.trigger.trigger_threshold = 0.5
    cfg.trigger.dedupe_window_seconds = 0.0
    holder = {"cfg": cfg}
    if audio is None:
        audio = np.concatenate([silence(0.6), speech(1.0), silence(0.9)])
    capture = FakeCapture(blocks_for(audio))
    recognizer = FakeRecognizer(text=text, error=recognizer_error)
    decisions: list = []
    statuses: list = []
    pipeline = DetectionPipeline(
        config_provider=lambda: holder["cfg"],
        capture=capture,
        recognizer=recognizer,
        keyword_detector=KeywordDetector(cfg.keywords),
        rule_analyzer=RuleAnalyzer(cfg.rules),
        trigger_engine=TriggerEngine(lambda: holder["cfg"]),
        ai_analyzer=ai_analyzer,
        state_machine=state_machine,
        on_status=statuses.append,
        on_trigger=lambda decision, candidate: decisions.append((decision, candidate)),
    )
    return pipeline, capture, recognizer, decisions, statuses, holder


def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_pipeline_triggers_on_keyword_sentence():
    pipeline, capture, recognizer, decisions, statuses, _holder = build_pipeline()
    pipeline.start()
    try:
        assert wait_until(lambda: decisions)
    finally:
        pipeline.stop()

    assert capture.started and capture.stopped
    assert recognizer.calls >= 1
    decision, candidate = decisions[0]
    assert decision.trigger
    assert "别管我" in candidate.text
    assert candidate.keyword is not None and candidate.keyword.matched
    assert candidate.features is not None and candidate.features.duration_s > 0.5
    assert any(status.kind == "triggered" for status in statuses)


def test_pipeline_does_not_trigger_for_plain_chat():
    pipeline, capture, _recognizer, decisions, statuses, _holder = build_pipeline(
        text="今天中午吃什么"
    )
    pipeline.start()
    try:
        assert wait_until(lambda: any(status.kind == "rejected" for status in statuses))
    finally:
        pipeline.stop()
    assert not decisions
    assert capture.stopped


def test_pipeline_survives_recognizer_failure():
    pipeline, _capture, _recognizer, decisions, statuses, holder = build_pipeline(
        recognizer_error=RecognizerError("模型缺失")
    )
    holder["cfg"].trigger.probabilities["serious"] = 0.0  # 只验证不崩溃
    # 低置信度档也要归零，否则合成分落在 0.35~0.5 时会随机触发
    holder["cfg"].trigger.probabilities["low_confidence"] = 0.0
    pipeline.start()
    try:
        assert wait_until(lambda: any(status.kind == "rejected" for status in statuses))
    finally:
        pipeline.stop()
    assert not decisions, "识别失败时不应凭噪声触发"


def test_pipeline_requires_stt_enabled_to_be_useful():
    """关闭识别时关键词层没有文本，仍然不崩，只是分数来自规则层。"""
    config = AppConfig()
    config.stt.enabled = False
    pipeline, _capture, recognizer, decisions, statuses, holder = build_pipeline(
        config=config, stt_enabled=False
    )
    holder["cfg"].trigger.probabilities["serious"] = 0.0
    # 低置信度档也要归零，否则合成分落在 0.35~0.5 时会随机触发
    holder["cfg"].trigger.probabilities["low_confidence"] = 0.0
    pipeline.start()
    try:
        assert wait_until(lambda: any(status.kind == "rejected" for status in statuses))
    finally:
        pipeline.stop()
    assert recognizer.calls == 0, "未启用识别时不应调用识别器"
    assert not decisions


def test_pipeline_moves_state_machine_through_candidate():
    machine = StateMachine()
    pipeline, _capture, _recognizer, decisions, statuses, _holder = build_pipeline(
        state_machine=machine, text="天气不错"
    )
    pipeline.start()
    try:
        assert wait_until(lambda: any(status.kind == "rejected" for status in statuses))
    finally:
        pipeline.stop()
    states = [change.new for change in machine.history]
    assert EventState.CANDIDATE in states, "检测到语音时应先进入候选状态"
    assert machine.state == EventState.IDLE
    assert not decisions


def test_pipeline_stop_releases_capture():
    pipeline, capture, _recognizer, _decisions, _statuses, _holder = build_pipeline()
    pipeline.start()
    assert pipeline.is_running
    pipeline.stop()
    assert not pipeline.is_running
    assert capture.stopped


def test_vad_config_follows_sensitivity():
    config = AppConfig()
    config.mic.vad_sensitivity = 0.0
    strict = make_vad_config(config)
    config.mic.vad_sensitivity = 1.0
    loose = make_vad_config(config)
    assert loose.absolute_floor_db < strict.absolute_floor_db, "灵敏度越高门限越低"


@pytest.mark.parametrize("text", ["", "   "])
def test_empty_transcript_does_not_trigger(text):
    pipeline, _capture, _recognizer, decisions, statuses, holder = build_pipeline(text=text)
    holder["cfg"].trigger.probabilities["serious"] = 0.0
    # 低置信度档也要归零，否则合成分落在 0.35~0.5 时会随机触发
    holder["cfg"].trigger.probabilities["low_confidence"] = 0.0
    pipeline.start()
    try:
        assert wait_until(lambda: any(status.kind == "rejected" for status in statuses))
    finally:
        pipeline.stop()
    assert not decisions


def test_pipeline_passes_recent_context_to_ai():
    """AI 层应拿到最近几句的上下文（不含当前句）。"""
    from fakes import FakeAnalyzer

    analyzer = FakeAnalyzer()
    config = AppConfig()
    config.ai.enabled = True
    config.ai.backend = "local_llm"
    config.ai.model = "fake-model"
    config.ai.max_context = 2
    # 关掉关键词层：关键词强命中会走快速通道跳过 AI，这里要验证的是 AI 的上下文传递
    config.keywords.enabled = False
    two_utterances = np.concatenate(
        [
            silence(0.5),
            speech(0.9),
            silence(0.8),
            speech(0.9),
            silence(0.9),
        ]
    )
    pipeline, _capture, _recognizer, _decisions, _statuses, holder = build_pipeline(
        text="我尽力了，替我报仇", config=config, ai_analyzer=analyzer, audio=two_utterances
    )
    pipeline.start()
    try:
        assert wait_until(lambda: len(analyzer.calls) >= 2)
    finally:
        pipeline.stop()

    first_text, first_context = analyzer.calls[0]
    second_text, second_context = analyzer.calls[1]
    assert first_context == [], "第一句还没有上下文"
    assert second_context == [first_text], "第二句应带上第一句"
    assert second_text == first_text


def test_pipeline_falls_back_when_ai_fails():
    """AI 报错时仍能决策，不崩、不假装有 AI 分（走 ai_unavailable 概率档）。"""
    from fakes import FakeAnalyzer
    from rusty_moment.detection.emotion_analyzer import AnalysisError

    analyzer = FakeAnalyzer(error=AnalysisError("AI 请求超时（4.0s）"))
    config = AppConfig()
    config.ai.enabled = True
    config.ai.backend = "local_llm"
    config.ai.model = "fake-model"
    # 关键词强命中会走快速通道跳过 AI，这里要验证 AI 失败路径，所以关掉关键词层
    config.keywords.enabled = False
    # 只剩规则层时分数偏低，把两个阈值降到 0，让本用例只验证「AI 缺失时的降级路径」
    config.trigger.trigger_threshold = 0.0
    config.trigger.low_confidence_threshold = 0.0
    # AI 启用但结果缺失时会走 ai_unavailable 概率档，这里固定成 100% 以免抽样造成 flaky
    config.trigger.probabilities["ai_unavailable"] = 1.0
    pipeline, _capture, _recognizer, decisions, statuses, holder = build_pipeline(
        config=config, ai_analyzer=analyzer
    )
    pipeline.start()
    try:
        assert wait_until(lambda: decisions)
    finally:
        pipeline.stop()

    decision, candidate = decisions[0]
    assert candidate.ai is None, "AI 失败时不应产生 AI 分数"
    assert "ai" not in decision.scores
    assert decision.trigger, "AI 缺失时应按 ai_unavailable 档照常触发"
    assert "超时" in candidate.ai_error

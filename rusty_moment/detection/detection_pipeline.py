"""检测流水线：麦克风 → VAD 切句 → 语音识别 → 关键词/规则/AI → 触发引擎。

线程模型：
- PortAudio 回调线程只把数据塞进队列；
- 本模块的 ``rusty-detect`` 工作线程负责切句、识别、打分（慢操作都在这里）；
- 决策结果通过回调交给 UI/EventManager（回调本身不应长时间阻塞）。

隐私：
- 原始音频默认只在内存里走动，处理完即丢弃；
- 只有用户显式打开 ``mic.save_raw_audio`` 时才会写 WAV；
- 转录文本默认不进日志（只记录长度与元数据）。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..config.config_manager import AppConfig
from ..core.state_machine import EventState, StateMachine
from .emotion_analyzer import AnalysisError, EmotionAnalyzer
from .keyword_detector import KeywordDetector, KeywordResult
from .microphone_capture import MicrophoneCapture, MicrophoneError
from .rule_analyzer import RuleAnalyzer, RuleResult
from .speech_recognizer import RecognizerError, SpeechRecognizer
from .trigger_engine import CandidateEvent, TriggerDecision, TriggerEngine
from .vad import Utterance, VadConfig, VoiceSegmenter

log = logging.getLogger(__name__)


class DetectionError(Exception):
    """检测流程启动失败。"""


@dataclass
class DetectionStatus:
    """给 UI 的观察数据（不含原始音频）。"""

    kind: str  # listening / candidate / rejected / triggered / error / stopped
    message: str = ""
    text: str = ""
    decision: TriggerDecision | None = None
    duration_s: float = 0.0


def default_recordings_dir() -> Path:
    import os

    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "RustyMoment" / "recordings"


class DetectionPipeline:
    def __init__(
        self,
        *,
        config_provider: Callable[[], AppConfig],
        capture: MicrophoneCapture,
        recognizer: SpeechRecognizer,
        keyword_detector: KeywordDetector,
        rule_analyzer: RuleAnalyzer,
        trigger_engine: TriggerEngine,
        ai_analyzer: EmotionAnalyzer | None = None,
        state_machine: StateMachine | None = None,
        on_status: Callable[[DetectionStatus], None] | None = None,
        on_trigger: Callable[[TriggerDecision, CandidateEvent], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config_provider
        self._capture = capture
        self._recognizer = recognizer
        self._keywords = keyword_detector
        self._rules = rule_analyzer
        self._engine = trigger_engine
        self._ai = ai_analyzer
        self._sm = state_machine
        self._on_status = on_status
        self._on_trigger = on_trigger
        self._clock = clock

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._segmenter: VoiceSegmenter | None = None
        self._lock = threading.RLock()
        self._last_status = DetectionStatus(kind="stopped")
        self._utterance_count = 0
        self._trigger_count = 0
        # 最近几句文本（只在内存里，给 AI 层做上下文；不落盘、不进日志）
        self._recent_texts: deque[str] = deque(maxlen=5)

    # -- 状态 --------------------------------------------------------------- #
    @property
    def is_running(self) -> bool:
        thread = self._thread
        return bool(thread and thread.is_alive())

    @property
    def last_status(self) -> DetectionStatus:
        return self._last_status

    @property
    def stats(self) -> dict[str, int]:
        return {"utterances": self._utterance_count, "triggers": self._trigger_count}

    # -- 生命周期 ----------------------------------------------------------- #
    def start(self) -> None:
        cfg = self._config()
        with self._lock:
            if self.is_running:
                return
            vad_config = make_vad_config(cfg)
            samplerate = int(cfg.mic.sample_rate or 16000)
            self._segmenter = VoiceSegmenter(samplerate, vad_config)
            self._recent_texts = deque(maxlen=max(1, int(cfg.ai.max_context or 1)))
            try:
                self._capture.start(
                    cfg.mic.device or None,
                    samplerate=samplerate,
                    blocksize=max(160, int(samplerate * 0.03)),
                )
            except MicrophoneError as exc:
                raise DetectionError(str(exc)) from exc
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="rusty-detect", daemon=True
            )
            self._thread.start()
        self._set_status("listening", f"正在监听麦克风（{samplerate} Hz）")

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)
        try:
            self._capture.stop()
        except Exception:  # pragma: no cover
            log.exception("停止麦克风失败")
        self._set_status("stopped", "已停止监听，麦克风已释放")

    def warmup_recognizer(self) -> None:
        """预加载语音模型（放后台线程调用，避免卡 UI）。"""
        try:
            self._recognizer.warmup()
        except RecognizerError as exc:
            log.warning("语音模型预热失败：%s", exc)
            self._set_status("error", f"语音模型不可用：{exc}")
        else:
            if self._recognizer.available:
                self._set_status("listening", "语音模型已就绪")

    def update_components(
        self,
        *,
        recognizer: SpeechRecognizer | None = None,
        ai_analyzer: EmotionAnalyzer | None = None,
    ) -> None:
        with self._lock:
            if recognizer is not None:
                self._recognizer = recognizer
            if ai_analyzer is not None:
                self._ai = ai_analyzer

    # -- 主循环 ------------------------------------------------------------- #
    def _run(self) -> None:
        segmenter = self._segmenter
        if segmenter is None:
            return
        while not self._stop.is_set():
            block = self._capture.read(timeout=0.3)
            if block is None:
                continue
            try:
                utterances = segmenter.push(block)
            except Exception:  # pragma: no cover - 切句不应崩
                log.exception("VAD 处理异常")
                continue
            for utterance in utterances:
                if self._stop.is_set():
                    break
                self._handle_utterance(utterance)
        # 收尾：把最后半句处理掉
        try:
            for utterance in segmenter.flush():
                self._handle_utterance(utterance)
        except Exception:  # pragma: no cover
            log.exception("收尾处理异常")

    def _handle_utterance(self, utterance: Utterance) -> None:
        cfg = self._config()
        self._utterance_count += 1
        if cfg.mic.save_raw_audio:
            self._save_recording(utterance)
        if self._sm is not None and self._sm.state in (EventState.IDLE, EventState.COOLDOWN):
            try:
                self._sm.transition(EventState.CANDIDATE, "检测到语音")
            except Exception:  # pragma: no cover - 状态机白名单已覆盖
                log.debug("无法进入候选状态", exc_info=True)

        text, transcript_error = "", ""
        if cfg.stt.enabled:
            try:
                transcript = self._recognizer.transcribe(utterance.pcm, utterance.samplerate)
                text = transcript.text
                log.debug(
                    "语音识别完成：%d 字，%.0fms", len(text), transcript.latency_ms
                )
            except RecognizerError as exc:
                transcript_error = str(exc)
                log.warning("语音识别失败，退回无文本模式：%s", exc)

        keyword: KeywordResult | None = None
        rule: RuleResult | None = None
        if cfg.keywords.enabled:
            keyword = self._keywords.analyze(text)
        if cfg.rules.enabled:
            rule = self._rules.analyze(utterance.features)

        ai_scores = None
        ai_error = ""
        # 关键词强命中时不等 AI：关键词是最硬的文本证据，AI 只是补充。
        # 这样「兄弟，别管我」这类台词能立刻触发，不用为一次云端往返多等 0.6~2 秒。
        keyword_strong = keyword is not None and keyword.matched and keyword.score >= 0.85
        if keyword_strong:
            log.debug("关键词强命中（%.2f），跳过 AI 层以降低延迟", keyword.score)
        elif cfg.ai.enabled and text:
            if self._ai is not None and self._ai.available:
                if cfg.ai.backend == "openai_compatible" and not cfg.privacy.cloud_consent:
                    ai_error = "未同意云端分析，已跳过 AI 层"
                else:
                    try:
                        ai_scores = self._ai.analyze(text, list(self._recent_texts))
                    except AnalysisError as exc:
                        ai_error = str(exc)
                        log.warning("AI 分析失败，退回规则模式：%s", exc)
            else:
                ai_error = "AI 未就绪（缺 key 或未配置模型）"

        if text:
            self._recent_texts.append(text)

        candidate = CandidateEvent(
            text=text,
            features=utterance.features,
            keyword=keyword,
            rule=rule,
            ai=ai_scores,
            ai_error=ai_error or transcript_error,
            transcript_source=cfg.stt.backend if cfg.stt.enabled else "none",
            at=self._clock(),
        )
        decision = self._engine.evaluate(candidate)

        if decision.trigger:
            self._trigger_count += 1
            self._set_status(
                "triggered",
                decision.reason,
                text=text if cfg.privacy.show_transcript_in_ui else "",
                decision=decision,
                duration=utterance.duration_s,
            )
            if self._on_trigger is not None:
                try:
                    self._on_trigger(decision, candidate)
                except Exception:  # pragma: no cover - 回调异常不应打断检测
                    log.exception("触发回调异常")
        else:
            self._set_status(
                "rejected",
                decision.reason,
                text=text if cfg.privacy.show_transcript_in_ui else "",
                decision=decision,
                duration=utterance.duration_s,
            )
            if self._sm is not None and self._sm.state == EventState.CANDIDATE:
                try:
                    self._sm.transition(EventState.IDLE, "候选事件未达标")
                except Exception:  # pragma: no cover
                    log.debug("候选回 idle 失败", exc_info=True)

    def _save_recording(self, utterance: Utterance) -> None:
        """仅在用户显式开启时写盘（默认关闭，属于调试功能）。"""
        try:
            import soundfile as sf

            target_dir = default_recordings_dir()
            target_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            path = target_dir / f"utt-{stamp}-{int(utterance.duration_s * 1000)}ms.wav"
            sf.write(str(path), utterance.pcm, utterance.samplerate)
            log.info("已保存录音片段（用户开启）：%s", path.name)
        except Exception:  # pragma: no cover - 调试功能失败不应影响主流程
            log.exception("保存录音片段失败")

    def _set_status(
        self,
        kind: str,
        message: str = "",
        *,
        text: str = "",
        decision: TriggerDecision | None = None,
        duration: float = 0.0,
    ) -> None:
        status = DetectionStatus(
            kind=kind, message=message, text=text, decision=decision, duration_s=duration
        )
        self._last_status = status
        if self._on_status is not None:
            try:
                self._on_status(status)
            except Exception:  # pragma: no cover
                log.exception("状态回调异常")


def make_vad_config(cfg: AppConfig) -> VadConfig:
    """从配置生成 VAD 参数（阶段 2 暴露了灵敏度与最短语音）。"""
    sensitivity = float(getattr(cfg.mic, "vad_sensitivity", 0.5) or 0.5)
    # 灵敏度越高，绝对门限越低（更容易判为语音）
    floor = -40.0 - 20.0 * max(0.0, min(1.0, sensitivity))
    return VadConfig(
        absolute_floor_db=floor,
        min_speech_ms=max(150, int(cfg.trigger.min_voice_seconds * 1000)),
    )

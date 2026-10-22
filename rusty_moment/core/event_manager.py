"""事件管理器：把「触发 → 切断游戏声 → 播放 BGM → 恢复 → 冷却」串成一条流程。

设计取舍：
- 一个事件 = 一条后台线程，事件内部严格串行，因此不会出现并发播放/交叉恢复。
- 恢复永远发生在播放流真正停止之后（``wait_until_inactive``），不会「音乐还在响
  游戏声就回来了」。
- 音乐播放期间的新触发默认被忽略（可配置为排队），不会叠加播放或覆盖快照。
- 所有对外回调都在事件线程里触发，UI 层负责转成 Qt 信号送回主线程。
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from ..audio.audio_player import (
    AudioDeviceError,
    AudioFileError,
    AudioPlayerError,
)
from ..audio.audio_state_manager import AudioStateError, AudioStateManager, RestoreOutcome
from ..config.config_manager import AppConfig
from ..obs.obs_client import OBSConnectionError, OBSRequestError, OBSTimeoutError
from ..obs.obs_controller import OBSSourceError
from .cooldown_manager import CooldownManager
from .state_machine import EventState, IllegalTransition, StateChange, StateMachine

log = logging.getLogger(__name__)

TRIGGER_MANUAL = "manual"


class PlayerLike(Protocol):
    @property
    def is_active(self) -> bool: ...

    def play(self, path: str | None = None, **kwargs: Any) -> Any: ...

    def stop(self) -> None: ...


@dataclass
class EventRecord:
    event_id: str
    started_at: float
    trigger_source: str
    reason: str
    mode: str
    score: float | None = None
    played_ok: bool | None = None
    restore_ok: bool | None = None
    obs_controlled: bool = False
    note: str = ""
    latency_ms: float | None = None


@dataclass
class EventResult:
    started: bool
    event_id: str = ""
    message: str = ""
    queued: bool = False


@dataclass
class EventCallbacks:
    """由 UI 层注入的回调集合（默认全部为空实现）。"""

    on_state_change: Callable[[StateChange], None] | None = None
    on_event_started: Callable[[EventRecord], None] | None = None
    on_event_finished: Callable[[EventRecord], None] | None = None
    on_log: Callable[[str, str], None] | None = None


class EventManager:
    def __init__(
        self,
        *,
        player: PlayerLike,
        control: Any,
        state_manager: AudioStateManager,
        state_machine: StateMachine,
        cooldown: CooldownManager,
        config_provider: Callable[[], AppConfig],
        callbacks: EventCallbacks | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._player = player
        self._control = control
        self._state_manager = state_manager
        self._sm = state_machine
        self._cooldown = cooldown
        self._config_provider = config_provider
        self._cb = callbacks or EventCallbacks()
        self._clock = clock
        self._sleep = sleep

        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop_requested = threading.Event()
        self._current: EventRecord | None = None
        self._queued: tuple[str, str, float | None] | None = None
        self._last_record: EventRecord | None = None

    # -- 查询 --------------------------------------------------------------- #
    @property
    def is_busy(self) -> bool:
        thread = self._thread
        return bool(thread and thread.is_alive())

    @property
    def current_event(self) -> EventRecord | None:
        return self._current

    @property
    def last_record(self) -> EventRecord | None:
        return self._last_record

    # -- 对外命令 ----------------------------------------------------------- #
    def trigger_manual(self, reason: str = "用户手动强制触发") -> EventResult:
        return self.start_event(TRIGGER_MANUAL, reason)

    def start_event(
        self, trigger_source: str, reason: str, score: float | None = None
    ) -> EventResult:
        cfg = self._config_provider()
        if cfg.mode.global_mute:
            return EventResult(False, message="全局静音开关已开启，本次触发被阻止")
        if trigger_source != TRIGGER_MANUAL and not cfg.mode.auto_trigger_enabled:
            return EventResult(False, message="自动触发已关闭，本次自动事件被忽略")

        with self._lock:
            if self.is_busy:
                if cfg.trigger.busy_policy == "queue":
                    self._queued = (trigger_source, reason, score)
                    self._log("info", "已有事件进行中，本次触发已排队")
                    return EventResult(False, message="已排队，等待当前事件结束", queued=True)
                self._log("info", "已有事件进行中，本次触发被忽略")
                return EventResult(False, message="已有事件进行中，本次触发被忽略")
            return self._launch(trigger_source, reason, score)

    def stop(self, reason: str = "用户停止音乐") -> bool:
        """请求停止：立即停音乐，后台线程负责恢复音频。"""
        thread = self._thread
        started = bool(thread and thread.is_alive())
        self._stop_requested.set()
        try:
            self._player.stop()
        except Exception as exc:  # pragma: no cover - 停止失败也要继续恢复
            log.warning("停止播放失败：%s", exc)
        if started:
            self._log("info", reason)
        return started

    def reset_audio_state(self) -> RestoreOutcome:
        """「重置音频状态」：按最近一次快照恢复被程序修改的音源。"""
        with self._lock:
            if self.is_busy:
                # 事件进行中先停，避免与事件线程抢同一份快照
                self.stop("重置音频状态：先停止当前事件")
                thread = self._thread
                if thread is not None:
                    thread.join(timeout=6.0)
            return self._state_manager.reset_to_snapshot()

    def skip_candidate(self, reason: str = "用户跳过本次候选事件") -> bool:
        if self._sm.state == EventState.CANDIDATE:
            self._sm.transition(EventState.IDLE, reason)
            self._log("info", reason)
            return True
        return False

    def test_audio(self, duration: float = 1.5) -> tuple[bool, str]:
        """测试音频链路：OBS 音源控制 + 音乐文件解码（纯本地模式只测播放）。"""
        cfg = self._config_provider()
        control_enabled = bool(getattr(cfg.obs, "control_enabled", True))
        problems: list[str] = []
        if not cfg.audio.bgm_path:
            problems.append("未选择 BGM 文件")
        if control_enabled and cfg.obs.game_source and not self._control.connected:
            problems.append("OBS 未连接，无法测试游戏音源控制")
        if control_enabled and not problems and cfg.obs.game_source:
            try:
                self._state_manager.begin("audio-test", cfg.obs.game_source)
                self._state_manager.apply_control(cfg.obs.control_strategy, cfg.obs.duck_volume)
                self._sleep(min(duration, 1.0))
                outcome = self._state_manager.restore()
                if not outcome.ok:
                    problems.append("游戏音源恢复失败：" + outcome.summary())
            except Exception as exc:
                problems.append(f"游戏音源控制失败：{exc}")
                try:
                    self._state_manager.release()
                except Exception:  # pragma: no cover
                    pass
        if not problems:
            try:
                self._player.play(
                    cfg.audio.bgm_path,
                    volume=cfg.audio.volume,
                    from_start=True,
                    loop=False,
                    device=cfg.audio.output_device,
                )
                self._sleep(min(duration, 1.0))
                self._player.stop()
            except (AudioFileError, AudioDeviceError, AudioPlayerError) as exc:
                problems.append(f"音乐播放失败：{exc}")
        if problems:
            return False, "；".join(problems)
        if control_enabled:
            return True, "音频测试通过：音源控制与音乐播放都正常"
        return True, "音频测试通过：纯本地模式，音乐播放正常（未使用 OBS）"

    # -- 内部 --------------------------------------------------------------- #
    def _launch(
        self, trigger_source: str, reason: str, score: float | None
    ) -> EventResult:
        cfg = self._config_provider()
        record = EventRecord(
            event_id=uuid.uuid4().hex[:12],
            started_at=time.time(),
            trigger_source=trigger_source,
            reason=reason,
            mode=cfg.mode.current,
            score=score,
        )
        self._current = record
        self._stop_requested.clear()
        if self._sm.state not in (EventState.IDLE, EventState.COOLDOWN):
            self._sm.force_reset("新事件开始前复位")
        thread = threading.Thread(
            target=self._run_event,
            args=(record,),
            name=f"rusty-event-{record.event_id}",
            daemon=True,
        )
        self._thread = thread
        self._log("info", f"事件 {record.event_id} 启动（{trigger_source}）：{reason}")
        if self._cb.on_event_started is not None:
            try:
                self._cb.on_event_started(record)
            except Exception:  # pragma: no cover
                log.exception("事件开始回调异常")
        thread.start()
        return EventResult(True, record.event_id, "已触发")

    def _run_event(self, record: EventRecord) -> None:
        cfg = self._config_provider()
        t0 = self._clock()
        obs_controlled = False
        notes: list[str] = []

        try:
            self._sm.transition(EventState.TRIGGERED, record.reason)

            game_source = cfg.obs.game_source
            if not cfg.obs.control_enabled:
                notes.append("纯本地模式：只播放音乐，不控制任何游戏音源")
                self._log("info", "纯本地模式：只播放音乐")
            elif game_source and self._control.connected:
                try:
                    self._state_manager.begin(record.event_id, game_source)
                    self._state_manager.apply_control(
                        cfg.obs.control_strategy, cfg.obs.duck_volume
                    )
                    obs_controlled = True
                except (
                    AudioStateError,
                    OBSSourceError,
                    OBSConnectionError,
                    OBSRequestError,
                    OBSTimeoutError,
                ) as exc:
                    notes.append(f"OBS 控制失败：{exc}")
                    self._log("error", f"OBS 控制失败：{exc}")
                    try:
                        if self._state_manager.is_active:
                            self._state_manager.restore()
                    except Exception:  # pragma: no cover - 尽力回滚
                        log.exception("回滚音源状态失败")
            elif not game_source:
                notes.append("未配置游戏音源：仅播放音乐，未控制游戏声音")
                self._log("warning", "未配置游戏音源：仅播放音乐")
            else:
                notes.append("OBS 未连接：仅本地播放，未控制游戏音频")
                self._log("warning", "OBS 未连接：仅本地播放，未控制游戏音频")

            record.obs_controlled = obs_controlled
            self._player.play(
                cfg.audio.bgm_path,
                volume=cfg.audio.volume,
                from_start=cfg.audio.play_from_start,
                loop=cfg.audio.allow_repeat,
                device=cfg.audio.output_device,
            )
            record.played_ok = True
            record.latency_ms = (self._clock() - t0) * 1000.0
            self._sm.transition(EventState.PLAYING, "BGM 开始播放")
            self._wait_playback(cfg)
        except (AudioFileError, AudioDeviceError, AudioPlayerError) as exc:
            record.played_ok = False
            notes.append(f"播放失败：{exc}")
            self._log("error", f"播放失败：{exc}")
        except IllegalTransition as exc:  # pragma: no cover - 防御性
            notes.append(f"状态异常：{exc}")
            self._log("error", f"状态异常：{exc}")
        except Exception as exc:  # pragma: no cover - 兜底
            record.played_ok = False
            notes.append(f"未预期错误：{exc}")
            log.exception("事件执行未预期错误")

        # ---- 恢复 ----
        if self._sm.state == EventState.PLAYING:
            try:
                self._sm.transition(EventState.RESTORING, "播放结束，恢复音频")
            except IllegalTransition:  # pragma: no cover
                self._sm.force_reset("播放结束后状态异常")
        elif obs_controlled and self._sm.state == EventState.TRIGGERED:
            try:
                self._sm.transition(EventState.RESTORING, "播放失败，回滚音频")
            except IllegalTransition:  # pragma: no cover
                self._sm.force_reset("回滚状态异常")

        if obs_controlled:
            outcome = self._state_manager.restore()
            record.restore_ok = outcome.ok
            summary = outcome.summary()
            if summary:
                notes.append(summary)
        else:
            record.restore_ok = None

        self._finish(record, notes, cfg)

    def _wait_playback(self, cfg: AppConfig) -> None:
        deadline = self._clock() + cfg.trigger.max_event_seconds
        while self._clock() < deadline:
            if self._stop_requested.is_set():
                return
            if not self._player.is_active:
                return
            self._sleep(0.03)
        # 超时保护：不让 BGM 无限播放
        self._log("warning", "播放超过最大时长，强制停止")
        self._player.stop()

    def _finish(self, record: EventRecord, notes: list[str], cfg: AppConfig) -> None:
        record.note = "；".join(item for item in notes if item)
        with self._lock:
            self._last_record = record
            self._current = None
            # 当前线程即将退出，先解除占用，排队的事件才能立刻启动
            self._thread = None
            queued = self._queued
            self._queued = None
        try:
            if self._sm.state in (EventState.RESTORING, EventState.TRIGGERED):
                self._sm.transition(EventState.COOLDOWN, "进入冷却")
            elif self._sm.state == EventState.IDLE:
                self._sm.force_reset("事件异常结束")
            if self._sm.state == EventState.COOLDOWN:
                self._cooldown.start(cfg.trigger.cooldown_seconds)
        except IllegalTransition:  # pragma: no cover
            self._sm.force_reset("收尾状态异常")
        self._log(
            "info",
            f"事件 {record.event_id} 结束：播放{'成功' if record.played_ok else '失败'}，"
            f"恢复{'成功' if record.restore_ok is not False else '失败'}",
        )
        if self._cb.on_event_finished is not None:
            try:
                self._cb.on_event_finished(record)
            except Exception:  # pragma: no cover
                log.exception("事件结束回调异常")
        self._schedule_idle()
        if queued is not None:
            trigger_source, reason, score = queued
            self._log("info", "执行排队中的触发")
            self.start_event(trigger_source, reason, score)

    def _schedule_idle(self) -> None:
        delay = self._cooldown.remaining()

        def worker() -> None:
            if delay > 0:
                self._sleep(delay + 0.01)
            with self._lock:
                if self.is_busy:
                    return
            if self._sm.state == EventState.COOLDOWN:
                try:
                    self._sm.transition(EventState.IDLE, "冷却结束")
                except IllegalTransition:  # pragma: no cover
                    pass

        threading.Thread(target=worker, name="rusty-cooldown", daemon=True).start()

    def _log(self, level: str, message: str) -> None:
        getattr(log, level if level in ("info", "warning", "error", "debug") else "info")(
            message
        )
        if self._cb.on_log is not None:
            try:
                self._cb.on_log(level, message)
            except Exception:  # pragma: no cover
                log.exception("日志回调异常")

"""应用装配：把配置、OBS、音频、状态机、事件管理器与界面接起来。

这里也是唯一的「依赖注入点」：测试可以构造只含纯逻辑组件的上下文，不需要 Qt。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .audio.audio_player import AudioPlayer, AudioPlayerError
from .audio.audio_state_manager import AudioStateManager
from .config.config_manager import AppConfig, ConfigCorruptError, ConfigManager
from .core.connection_watcher import ConnectionWatcher
from .core.cooldown_manager import CooldownManager
from .core.event_manager import EventCallbacks, EventManager
from .core.global_hotkeys import GlobalHotkeyManager, build_binding_map
from .core.state_machine import StateMachine
from .detection.ai_backends import build_analyzer
from .detection.detection_pipeline import DetectionPipeline
from .detection.keyword_detector import KeywordDetector
from .detection.microphone_capture import MicrophoneCapture
from .detection.rule_analyzer import RuleAnalyzer
from .detection.speech_recognizer import NullRecognizer, SpeechRecognizer, build_recognizer
from .phrases import library_from_legacy_keywords, load_or_default, merge_libraries, summarize
from .phrases.combination import RuleEngine
from .phrases.context import ContextTracker
from .phrases.matcher import PhraseMatcher
from .detection.trigger_engine import CandidateEvent, DedupeTracker, TriggerDecision, TriggerEngine
from .obs.obs_client import ConnectionState, OBSClient
from .obs.obs_controller import OBSController
from .security.secret_store import SecretStore
from .utils.logging_setup import setup_logging

log = logging.getLogger(__name__)

OBS_PASSWORD_KEY = "obs_password"
AI_API_KEY = "ai_api_key"


def trigger_source_label(decision: TriggerDecision) -> str:
    """把决策里实际参与打分的层转成日志/表格里的「触发方式」。"""
    layers = set(decision.scores)
    if "ai" in layers:
        return "ai"
    if layers == {"keyword"}:
        return "keyword"
    if layers == {"rule"}:
        return "rule"
    if {"keyword", "rule"} <= layers:
        return "keyword+rule"
    return "auto"


def recognizer_signature(cfg: AppConfig) -> str:
    s = cfg.stt
    return f"{s.enabled}|{s.backend}|{s.model_size}|{s.language}|{s.model_dir}|{s.compute_type}"


@dataclass
class ApplicationContext:
    """运行期依赖容器。"""

    config: AppConfig
    config_manager: ConfigManager
    secret_store: SecretStore
    player: AudioPlayer
    obs_client: OBSClient
    obs_controller: OBSController
    state_manager: AudioStateManager
    state_machine: StateMachine
    cooldown: CooldownManager
    event_manager: EventManager
    bridge: Any = None
    watcher: ConnectionWatcher | None = None
    # EventManager 通过 config_provider 取配置，所以共享同一个可变持有者
    config_holder: dict[str, AppConfig] = field(default_factory=dict)
    # 检测链（阶段 2）
    keywords: KeywordDetector | None = None
    rules: RuleAnalyzer | None = None
    trigger_engine: TriggerEngine | None = None
    phrase_library: Any = None
    """结构化词库（阶段 2/3），供重建识别器时生成热词提示。"""
    capture: MicrophoneCapture | None = None
    recognizer: SpeechRecognizer | None = None
    ai_analyzer: Any = None
    detection: DetectionPipeline | None = None
    recognizer_signature: str = ""
    ai_key_override: str = ""
    """用户输入但选择「不保存」的 API Key，只留在内存里。"""
    hotkeys: GlobalHotkeyManager | None = None

    # -- 配置 --------------------------------------------------------------- #
    def save_config(self) -> None:
        self.config_manager.save(self.config)

    def apply_config(
        self,
        new_config: AppConfig,
        *,
        password: str | None = None,
        save_password: bool = False,
        ai_key: str | None = None,
        save_ai_key: bool = False,
    ) -> None:
        """设置对话框确认后调用：落盘 + 让各组件立即生效。"""
        old_host, old_port = self.obs_client.host, self.obs_client.port
        self.config = new_config
        self.config_holder["cfg"] = new_config
        self.config_manager.save(new_config)
        cfg = new_config.obs
        self.obs_client.host, self.obs_client.port = cfg.host, cfg.port
        self.state_manager.restore_strategy = cfg.restore_strategy
        if password is not None:
            self.set_obs_password(password, persist=save_password)
        if ai_key is not None:
            self.set_ai_key(ai_key, persist=save_ai_key)
        if self.obs_client.connected and (old_host, old_port) != (cfg.host, cfg.port):
            log.info("OBS 连接参数已变更，断开旧连接")
            self.obs_controller.disconnect()
        if self.keywords is not None:
            self.keywords.update_config(new_config.keywords)
        if self.rules is not None:
            self.rules.update_config(new_config.rules)
        if not new_config.obs.control_enabled and self.obs_client.connected:
            log.info("已切到纯本地模式，断开 OBS 连接")
            self.obs_controller.disconnect()
        self.rebuild_recognizer()
        self.rebuild_analyzer()
        self.apply_global_hotkeys()

    def apply_global_hotkeys(self) -> None:
        """按配置注册/注销全局热键（默认关闭，开启会独占按键）。"""
        if self.hotkeys is None:
            return
        cfg = self.config.hotkeys
        if not cfg.enabled or not cfg.global_enabled:
            self.hotkeys.stop()
            return
        failures = self.hotkeys.start(build_binding_map(cfg))
        for item in failures:
            log.warning("全局热键：%s", item)
            if self.bridge is not None:
                self.bridge.log_message.emit("warning", f"全局热键：{item}")
        if not failures and self.bridge is not None:
            self.bridge.log_message.emit(
                "info", "全局热键已生效（游戏里也会拦截这些按键，注意冲突）"
            )

    def set_obs_password(self, password: str, *, persist: bool) -> None:
        if persist:
            try:
                self.secret_store.set_secret(OBS_PASSWORD_KEY, password)
            except Exception as exc:
                log.warning("保存 OBS 密码失败：%s", exc)
                if self.bridge is not None:
                    self.bridge.log_message.emit("warning", f"保存 OBS 密码失败：{exc}")
        self.obs_client.set_password_provider(lambda: password)

    # -- 检测链 ------------------------------------------------------------- #
    def rebuild_recognizer(self) -> None:
        """按当前 STT 配置重建识别器（用户改设置时才需要，模型重载较慢）。"""
        signature = recognizer_signature(self.config)
        if signature == self.recognizer_signature and self.recognizer is not None:
            return
        model_dir = self.config.stt.model_dir or None
        stt_config = self.config.stt
        # 词库热词：把高频短表达塞进 whisper 的 initial_prompt，提高专有词识别率
        if stt_config.hotwords_enabled and self.phrase_library is not None:
            from .phrases.hotwords import build_stt_hint, merge_prompt

            hint = build_stt_hint(self.phrase_library)
            merged = merge_prompt(stt_config.initial_prompt, hint)
            if merged and merged != stt_config.initial_prompt:
                stt_config = replace(stt_config, initial_prompt=merged)
                log.info("语音识别提示词已加入词库热词（%d 字）", len(merged))
        self.recognizer = build_recognizer(stt_config, model_dir=model_dir)
        self.recognizer_signature = signature
        if self.detection is not None:
            self.detection.update_components(recognizer=self.recognizer)

    def set_ai_key(self, key: str, *, persist: bool) -> None:
        self.ai_key_override = key or ""
        if persist and key:
            try:
                self.secret_store.set_secret(AI_API_KEY, key)
            except Exception as exc:
                log.warning("保存 AI API Key 失败：%s", exc)
                if self.bridge is not None:
                    self.bridge.log_message.emit("warning", f"保存 AI API Key 失败：{exc}")

    def current_ai_key(self) -> str:
        if self.ai_key_override:
            return self.ai_key_override
        try:
            return self.secret_store.get_secret(AI_API_KEY) or ""
        except Exception:  # pragma: no cover
            return ""

    def rebuild_analyzer(self) -> None:
        """按当前 AI 配置重建语义分析器（未启用时是 NullAnalyzer）。"""
        self.ai_analyzer = build_analyzer(self.config.ai, self.current_ai_key)
        if self.detection is not None:
            self.detection.update_components(ai_analyzer=self.ai_analyzer)

    def start_listening(self) -> None:
        if self.detection is None:
            raise RuntimeError("检测链未初始化")
        self.detection.start()

    def stop_listening(self) -> None:
        if self.detection is not None:
            self.detection.stop()

    # -- OBS ---------------------------------------------------------------- #
    def reconnect_obs(self) -> str:
        cfg = self.config.obs
        if not cfg.control_enabled:
            raise RuntimeError(
                "当前是「仅本地播歌」模式，不会连接 OBS；"
                "需要控制游戏音源时请先把输出方式切回 OBS。"
            )
        self.obs_client.host, self.obs_client.port = cfg.host, cfg.port
        return self.obs_controller.connect()

    # -- 生命周期 ----------------------------------------------------------- #
    def start_background(self) -> None:
        if self.watcher is not None:
            self.watcher.start()

    def shutdown(self) -> None:
        """退出清理：停监听、停音乐、尽力恢复音频、断开 OBS、停守护线程。"""
        log.info("开始关闭 Rusty Moment")
        try:
            self.stop_listening()
        except Exception:  # pragma: no cover
            log.exception("退出时停止监听失败")
        try:
            if self.hotkeys is not None:
                self.hotkeys.stop()
        except Exception:  # pragma: no cover
            log.exception("退出时注销全局热键失败")
        if self.watcher is not None:
            self.watcher.stop()
        try:
            self.event_manager.stop("程序退出")
        except Exception:  # pragma: no cover
            log.exception("退出时停止事件失败")
        try:
            if self.state_manager.is_active and self.obs_client.connected:
                outcome = self.state_manager.restore()
                log.info("退出时恢复音频：%s", outcome.summary())
        except Exception:  # pragma: no cover
            log.exception("退出时恢复音频失败")
        try:
            self.player.close()
        except Exception:  # pragma: no cover
            log.exception("退出时释放播放器失败")
        try:
            self.obs_controller.disconnect()
        except Exception:  # pragma: no cover
            log.exception("退出时断开 OBS 失败")


def build_context(
    config: AppConfig,
    config_manager: ConfigManager,
    *,
    secret_store: SecretStore | None = None,
    bridge: Any = None,
    player: AudioPlayer | None = None,
    obs_client: OBSClient | None = None,
    state_machine: StateMachine | None = None,
) -> ApplicationContext:
    """构造除界面以外的所有组件。"""
    secrets = secret_store or SecretStore()
    audio_player = player or AudioPlayer()

    def password_provider() -> str | None:
        try:
            return secrets.get_secret(OBS_PASSWORD_KEY)
        except Exception:  # pragma: no cover
            return None

    def on_obs_state(state: ConnectionState, detail: str) -> None:
        if bridge is not None:
            bridge.connection_changed.emit(state == ConnectionState.CONNECTED, detail)

    client = obs_client or OBSClient(
        host=config.obs.host,
        port=config.obs.port,
        password_provider=password_provider,
    )
    client.on_state_change = on_obs_state
    controller = OBSController(client)

    machine = state_machine or StateMachine(
        on_change=(bridge.state_changed.emit if bridge is not None else None)
    )
    cooldown = CooldownManager()
    state_manager = AudioStateManager(controller, restore_strategy=config.obs.restore_strategy)
    callbacks = EventCallbacks(
        on_event_started=(bridge.event_started.emit if bridge is not None else None),
        on_event_finished=(bridge.event_finished.emit if bridge is not None else None),
        on_log=(bridge.log_message.emit if bridge is not None else None),
    )

    config_holder = {"cfg": config}

    def config_provider() -> AppConfig:
        return config_holder["cfg"]

    event_manager = EventManager(
        player=audio_player,
        control=controller,
        state_manager=state_manager,
        state_machine=machine,
        cooldown=cooldown,
        config_provider=config_provider,
        callbacks=callbacks,
    )

    # ---- 检测链（阶段 2）：关键词 + 规则，AI 层先留空实现 ----
    keywords = KeywordDetector(config.keywords)
    rules = RuleAnalyzer(config.rules)
    dedupe = DedupeTracker(max(0.0, config.trigger.dedupe_window_seconds))
    trigger_engine = TriggerEngine(
        config_provider, cooldown=cooldown, dedupe=dedupe, clock=time.monotonic
    )
    capture = MicrophoneCapture()
    ai_analyzer: Any = build_analyzer(
        config.ai, lambda: secrets.get_secret(AI_API_KEY) or ""
    )

    def on_detection_trigger(decision: TriggerDecision, candidate: CandidateEvent) -> None:
        event_manager.start_event(
            trigger_source_label(decision), decision.reason, score=decision.combined
        )

    # ---- 结构化词库（阶段 3）：词库匹配 + 组合规则 + 上下文 ----
    library, library_errors, _library_warnings = load_or_default(
        config.phrases.library_path or None
    )
    if library_errors:
        log.warning("词库加载有问题，已回退内置词库：%s", "；".join(library_errors[:3]))
    # 旧版 keywords 里的自建词条并入（只补缺失表达，不覆盖内置的类别与权重）
    legacy_library = library_from_legacy_keywords(
        config.keywords.positives, config.keywords.exclusions
    )
    if legacy_library.entries:
        library = merge_libraries(library, legacy_library, overwrite=False)
        log.info("已把 %d 条旧关键词并入结构化词库（不覆盖内置表达）", len(legacy_library.entries))
    phrase_matcher = PhraseMatcher(
        library,
        case_sensitive=config.keywords.case_sensitive,
        min_hits=config.keywords.min_hits,
        fuzzy=config.phrases.fuzzy_enabled,
    )
    phrase_rules = RuleEngine(library)
    context_tracker = ContextTracker(window_seconds=config.phrases.context_window_seconds)
    log.info("词库就绪：%s", summarize(library))

    detection = DetectionPipeline(
        config_provider=config_provider,
        capture=capture,
        recognizer=NullRecognizer(),
        keyword_detector=keywords,
        rule_analyzer=rules,
        trigger_engine=trigger_engine,
        ai_analyzer=ai_analyzer,
        state_machine=machine,
        on_status=(bridge.detection_status.emit if bridge is not None else None),
        on_trigger=on_detection_trigger,
        phrase_matcher=phrase_matcher,
        rule_engine=phrase_rules,
        context_tracker=context_tracker,
    )

    ctx = ApplicationContext(
        config=config,
        config_manager=config_manager,
        secret_store=secrets,
        player=audio_player,
        obs_client=client,
        obs_controller=controller,
        state_manager=state_manager,
        state_machine=machine,
        cooldown=cooldown,
        event_manager=event_manager,
        bridge=bridge,
        config_holder=config_holder,
        keywords=keywords,
        rules=rules,
        trigger_engine=trigger_engine,
        capture=capture,
        ai_analyzer=ai_analyzer,
        detection=detection,
        phrase_library=library,
    )
    ctx.rebuild_recognizer()
    ctx.rebuild_analyzer()
    ctx.hotkeys = GlobalHotkeyManager(
        on_action=(bridge.global_hotkey.emit if bridge is not None else None)
    )
    ctx.apply_global_hotkeys()

    watcher = ConnectionWatcher(
        connect_fn=ctx.reconnect_obs,
        is_connected=lambda: client.connected,
        enabled=lambda: config_holder["cfg"].obs.auto_reconnect
        and config_holder["cfg"].obs.control_enabled,
        interval=lambda: config_holder["cfg"].obs.reconnect_interval_s,
        on_connected=lambda info: bridge.connection_changed.emit(True, info) if bridge else None,
        on_failed=lambda msg: bridge.connection_changed.emit(False, msg) if bridge else None,
    )
    ctx.watcher = watcher
    return ctx


def create_application(argv: list[str] | None = None) -> tuple[Any, ApplicationContext, Any]:
    """创建 QApplication + 上下文 + 主窗口。返回 (app, ctx, window)。"""
    from PySide6.QtWidgets import QApplication, QMessageBox

    from .ui.main_window import MainWindow
    from .ui.qt_bridge import EventBridge

    app = QApplication(argv if argv is not None else [])
    app.setApplicationName("Rusty Moment")

    manager = ConfigManager()
    try:
        config = manager.load()
    except ConfigCorruptError as exc:
        QMessageBox.warning(
            None,
            "配置损坏",
            f"配置文件无法使用：{exc}\n"
            + (f"\n原文件已备份到：\n{exc.backup_path}" if exc.backup_path else "")
            + "\n\n将恢复默认配置并继续启动。",
        )
        config = manager.reset_to_default()

    setup_logging(config.logging.level, Path(config.logging.dir) if config.logging.dir else None)

    try:
        SecretStore().get_secret(OBS_PASSWORD_KEY)  # 预热，失败仅在日志里提示
        bridge = EventBridge()
        ctx = build_context(config, manager, bridge=bridge)
    except AudioPlayerError as exc:
        QMessageBox.critical(None, "音频初始化失败", f"{exc}\n\n请检查系统音频设备后重试。")
        raise

    window = MainWindow(ctx)
    pending = AudioStateManager.load_pending()
    if pending:
        window.notify_pending_state(pending)
    ctx.start_background()
    return app, ctx, window


def run(argv: list[str] | None = None) -> int:
    from PySide6.QtWidgets import QMessageBox

    try:
        app, ctx, window = create_application(argv)
    except Exception as exc:  # pragma: no cover - 启动失败兜底
        log.exception("启动失败")
        try:
            QMessageBox.critical(None, "启动失败", str(exc))
        except Exception:
            print(f"启动失败：{exc}")
        return 1
    window.show()
    exit_code = app.exec()
    try:
        ctx.shutdown()
    except Exception:  # pragma: no cover
        log.exception("退出清理失败")
    return int(exit_code)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run())

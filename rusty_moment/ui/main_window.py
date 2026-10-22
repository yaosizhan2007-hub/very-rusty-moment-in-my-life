"""主界面：状态、核心控制、模式设置、事件日志。

线程约定：所有事件/OBS 操作都通过 ``run_async`` 或 EventManager 的后台线程执行，
界面线程只负责展示与写入配置。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCloseEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..audio.music_library import track_title
from ..config.config_manager import AppConfig
from ..core.event_manager import EventRecord
from ..core.state_machine import STATE_LABELS, EventState, StateChange
from .event_log_widget import EventLogWidget
from .qt_bridge import EventBridge, run_async
from .settings_dialog import SettingsDialog

log = logging.getLogger(__name__)

DARK_STYLE = """
QMainWindow, QDialog, QWidget { background-color: #12131a; color: #e6e6f0; }
QLabel#title { font-size: 20px; font-weight: 700; color: #ffd166; }
QLabel#badge { padding: 2px 8px; border-radius: 8px; background: #1f2233; }
QGroupBox { border: 1px solid #2a2e45; border-radius: 8px; margin-top: 12px; padding: 8px; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; color: #9aa0c0; }
QPushButton { background: #232744; border: 1px solid #343a5e; border-radius: 6px; padding: 6px 14px; }
QPushButton:hover { background: #2c3157; }
QPushButton:pressed { background: #1a1d33; }
QPushButton#danger { background: #6d1f2c; border-color: #a02a3a; font-weight: 700; }
QPushButton#danger:hover { background: #86263a; }
QPushButton#force { background: #7a3b00; border-color: #ff8c00; font-size: 18px; font-weight: 700; }
QPushButton#force:hover { background: #944a00; }
QProgressBar { border: 1px solid #343a5e; border-radius: 4px; text-align: center; }
QProgressBar::chunk { background-color: #ff8c00; }
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox { background: #1b1e30; border: 1px solid #343a5e; border-radius: 4px; padding: 3px; }
QTableWidget { background: #15172300; alternate-background-color: #1b1e30; gridline-color: #2a2e45; }
QHeaderView::section { background: #1b1e30; color: #9aa0c0; padding: 4px; border: none; }
"""


class MainWindow(QMainWindow):
    def __init__(self, ctx: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.bridge: EventBridge = ctx.bridge
        self.setWindowTitle("Rusty Moment · 悲壮瞬间自动插曲器")
        self.resize(980, 720)
        self.setStyleSheet(DARK_STYLE)

        self._shortcuts: list[QShortcut] = []
        self._build_ui()
        self._connect_bridge()
        self._load_config_into_ui()
        self._install_shortcuts()

        self._timer = QTimer(self)
        self._timer.setInterval(200)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self._tick_timer = QTimer(self)
        self._tick_timer.setSingleShot(True)
        self._tick_timer.timeout.connect(self._prewarm_current_track)
        self._tick_timer.start(600)
        self._append_log("info", "Rusty Moment 已启动。仅控制 OBS 内部音源。")

    # ------------------------------------------------------------------ #
    # 构建界面
    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        central = QWidget(self)
        root = QVBoxLayout(central)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(10)

        root.addWidget(self._build_status_bar(central))

        middle = QHBoxLayout()
        middle.addWidget(self._build_core_panel(central), 3)
        middle.addWidget(self._build_mode_panel(central), 2)
        root.addLayout(middle)

        self.log_widget = EventLogWidget(self.ctx.config.logging.max_entries, central)
        log_box = QGroupBox("事件日志（仅记录元数据）", central)
        log_layout = QVBoxLayout(log_box)
        log_layout.addWidget(self.log_widget)
        actions = QHBoxLayout()
        clear_button = QPushButton("清除日志", log_box)
        clear_button.clicked.connect(self._on_clear_log)
        actions.addStretch(1)
        actions.addWidget(clear_button)
        log_layout.addLayout(actions)
        root.addWidget(log_box, 1)

        self.setCentralWidget(central)

    def _build_status_bar(self, parent: QWidget) -> QWidget:
        frame = QFrame(parent)
        frame.setObjectName("statusBar")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(4, 4, 4, 4)
        title = QLabel("RUSTY MOMENT", frame)
        title.setObjectName("title")
        layout.addWidget(title)

        self.obs_label = QLabel("OBS：未连接", frame)
        self.obs_label.setObjectName("badge")
        self.mic_label = QLabel("麦克风：未启用", frame)
        self.mic_label.setObjectName("badge")
        self.play_label = QLabel("播放：空闲", frame)
        self.play_label.setObjectName("badge")
        self.mode_label = QLabel("模式：智能悲壮", frame)
        self.mode_label.setObjectName("badge")
        self.state_label = QLabel("状态：空闲", frame)
        self.state_label.setObjectName("badge")
        for widget in (self.obs_label, self.mic_label, self.play_label, self.mode_label, self.state_label):
            layout.addWidget(widget)
        layout.addStretch(1)
        return frame

    def _build_core_panel(self, parent: QWidget) -> QWidget:
        box = QGroupBox("核心控制", parent)
        layout = QVBoxLayout(box)

        button_row = QHBoxLayout()
        self.force_button = QPushButton("强制触发（F8）", box)
        self.force_button.setObjectName("force")
        self.force_button.setMinimumHeight(64)
        self.force_button.clicked.connect(self._on_force_trigger)
        self.stop_button = QPushButton("停止音乐（F9）", box)
        self.stop_button.setObjectName("danger")
        self.stop_button.setMinimumHeight(64)
        self.stop_button.clicked.connect(self._on_stop)
        button_row.addWidget(self.force_button, 3)
        button_row.addWidget(self.stop_button, 2)
        layout.addLayout(button_row)

        self.auto_check = QCheckBox("启用自动触发（识别麦克风内容）", box)
        self.auto_check.toggled.connect(self._on_auto_toggled)
        layout.addWidget(self.auto_check)
        self.mute_check = QCheckBox("全局静音开关：阻止所有新的音乐事件", box)
        self.mute_check.toggled.connect(self._on_global_mute_toggled)
        layout.addWidget(self.mute_check)

        mic_row = QHBoxLayout()
        self.listen_button = QPushButton("开始监听麦克风", box)
        self.listen_button.clicked.connect(self._on_toggle_listen)
        self.transcript_label = QLabel("最近识别：—", box)
        self.transcript_label.setWordWrap(True)
        self.transcript_label.setStyleSheet("color: #9aa0c0;")
        mic_row.addWidget(self.listen_button)
        mic_row.addWidget(self.transcript_label, 1)
        layout.addLayout(mic_row)

        track_row = QHBoxLayout()
        track_row.addWidget(QLabel("悲壮曲目", box))
        self.track_combo = QComboBox(box)
        self.track_combo.setMinimumWidth(220)
        self.track_combo.currentIndexChanged.connect(self._on_track_changed)
        self.add_music_button = QPushButton("添加音乐…", box)
        self.add_music_button.clicked.connect(self._on_add_music)
        track_row.addWidget(self.track_combo, 1)
        track_row.addWidget(self.add_music_button)
        layout.addLayout(track_row)
        self.track_hint = QLabel("—", box)
        self.track_hint.setStyleSheet("color: #9aa0c0;")
        self.track_hint.setWordWrap(True)
        layout.addWidget(self.track_hint)

        self.progress = QProgressBar(box)
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress.setFormat("%p%")
        layout.addWidget(self.progress)

        info_grid = QGridLayout()
        info_grid.addWidget(QLabel("当前事件：", box), 0, 0)
        self.event_label = QLabel("—", box)
        info_grid.addWidget(self.event_label, 0, 1)
        info_grid.addWidget(QLabel("触发原因：", box), 1, 0)
        self.reason_label = QLabel("—", box)
        self.reason_label.setWordWrap(True)
        info_grid.addWidget(self.reason_label, 1, 1)
        info_grid.addWidget(QLabel("评分：", box), 2, 0)
        self.score_label = QLabel("—", box)
        info_grid.addWidget(self.score_label, 2, 1)
        info_grid.addWidget(QLabel("冷却：", box), 3, 0)
        self.cooldown_label = QLabel("就绪", box)
        info_grid.addWidget(self.cooldown_label, 3, 1)
        layout.addLayout(info_grid)

        ops = QGridLayout()
        self.connect_button = QPushButton("连接 / 重新连接 OBS", box)
        self.connect_button.clicked.connect(self._on_connect_obs)
        self.test_button = QPushButton("测试音频", box)
        self.test_button.clicked.connect(self._on_test_audio)
        self.reset_button = QPushButton("重置音频状态", box)
        self.reset_button.clicked.connect(self._on_reset_audio)
        self.skip_button = QPushButton("跳过本次候选", box)
        self.skip_button.clicked.connect(self._on_skip)
        self.settings_button = QPushButton("设置", box)
        self.settings_button.clicked.connect(self._on_settings)
        ops.addWidget(self.connect_button, 0, 0)
        ops.addWidget(self.test_button, 0, 1)
        ops.addWidget(self.reset_button, 1, 0)
        ops.addWidget(self.skip_button, 1, 1)
        ops.addWidget(self.settings_button, 2, 0, 1, 2)
        layout.addLayout(ops)
        layout.addStretch(1)
        return box

    def _build_mode_panel(self, parent: QWidget) -> QWidget:
        box = QGroupBox("模式与概率", parent)
        layout = QVBoxLayout(box)

        layout.addWidget(QLabel("输出方式", box))
        self.output_combo = QComboBox(box)
        self.output_combo.addItem("OBS 控制（切断/压低游戏声）", True)
        self.output_combo.addItem("仅本地播歌（不连 OBS）", False)
        self.output_combo.currentIndexChanged.connect(self._on_output_mode_changed)
        layout.addWidget(self.output_combo)
        self.output_hint = QLabel("—", box)
        self.output_hint.setWordWrap(True)
        self.output_hint.setStyleSheet("color: #9aa0c0;")
        layout.addWidget(self.output_hint)

        self.mode_combo = QComboBox(box)
        self.mode_combo.addItem("智能悲壮模式（认真识别戏剧性）", "serious")
        self.mode_combo.addItem("抽象整活模式（随机制造反差）", "chaotic")
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        layout.addWidget(QLabel("喜剧模式", box))
        layout.addWidget(self.mode_combo)

        layout.addWidget(QLabel("正常悲壮事件触发概率", box))
        self.prob_serious = self._percent_spin(box)
        self.prob_serious.valueChanged.connect(self._on_probability_changed)
        layout.addWidget(self.prob_serious)

        layout.addWidget(QLabel("低置信度事件触发概率", box))
        self.prob_low = self._percent_spin(box)
        self.prob_low.valueChanged.connect(self._on_probability_changed)
        layout.addWidget(self.prob_low)

        layout.addWidget(QLabel("抽象整活额外概率", box))
        self.prob_chaotic = self._percent_spin(box)
        self.prob_chaotic.valueChanged.connect(self._on_probability_changed)
        layout.addWidget(self.prob_chaotic)

        self.prob_hint = QLabel(
            "概率只对「满足条件的候选事件」抽样，不是每隔固定时间必触发一次；"
            "冷却时间与阈值仍然生效。",
            box,
        )
        self.prob_hint.setWordWrap(True)
        self.prob_hint.setStyleSheet("color: #9aa0c0;")
        layout.addWidget(self.prob_hint)

        layout.addWidget(QLabel("冷却时间（秒）", box))
        self.cooldown_spin = QDoubleSpinBox(box)
        self.cooldown_spin.setRange(0, 600)
        self.cooldown_spin.setDecimals(1)
        self.cooldown_spin.valueChanged.connect(self._on_timing_changed)
        layout.addWidget(self.cooldown_spin)

        layout.addWidget(QLabel("触发评分阈值", box))
        self.threshold_spin = QDoubleSpinBox(box)
        self.threshold_spin.setRange(0, 1)
        self.threshold_spin.setSingleStep(0.05)
        self.threshold_spin.setDecimals(2)
        self.threshold_spin.valueChanged.connect(self._on_timing_changed)
        layout.addWidget(self.threshold_spin)

        reset_button = QPushButton("恢复默认概率与阈值", box)
        reset_button.clicked.connect(self._on_reset_probabilities)
        layout.addWidget(reset_button)
        layout.addStretch(1)
        return box

    def _percent_spin(self, parent: QWidget) -> QDoubleSpinBox:
        spin = QDoubleSpinBox(parent)
        spin.setRange(0, 100)
        spin.setDecimals(0)
        spin.setSuffix(" %")
        return spin

    # ------------------------------------------------------------------ #
    # 配置 <-> 界面
    # ------------------------------------------------------------------ #
    @property
    def config(self) -> AppConfig:
        return self.ctx.config

    def _load_config_into_ui(self) -> None:
        cfg = self.config
        self._suspend_signals(True)
        self.auto_check.setChecked(cfg.mode.auto_trigger_enabled)
        self.mute_check.setChecked(cfg.mode.global_mute)
        output_index = self.output_combo.findData(bool(cfg.obs.control_enabled))
        self.output_combo.setCurrentIndex(max(0, output_index))
        index = self.mode_combo.findData(cfg.mode.current)
        self.mode_combo.setCurrentIndex(max(0, index))
        self.prob_serious.setValue(cfg.trigger.probabilities["serious"] * 100)
        self.prob_low.setValue(cfg.trigger.probabilities["low_confidence"] * 100)
        self.prob_chaotic.setValue(cfg.trigger.probabilities["chaotic"] * 100)
        self.cooldown_spin.setValue(cfg.trigger.cooldown_seconds)
        self.threshold_spin.setValue(cfg.trigger.trigger_threshold)
        self._suspend_signals(False)
        self._refresh_mode_label()
        self._update_shortcut_labels()
        self._refresh_track_combo()
        self._refresh_output_hint()
        self._refresh_obs_label()

    def _suspend_signals(self, suspend: bool) -> None:
        widgets = [
            self.auto_check,
            self.mute_check,
            self.output_combo,
            self.mode_combo,
            self.prob_serious,
            self.prob_low,
            self.prob_chaotic,
            self.cooldown_spin,
            self.threshold_spin,
        ]
        for widget in widgets:
            widget.blockSignals(suspend)

    def _save_config(self) -> None:
        try:
            self.ctx.save_config()
        except Exception as exc:
            self._append_log("error", f"保存配置失败：{exc}")

    def _refresh_mode_label(self) -> None:
        chaotic = self.config.mode.current == "chaotic"
        self.mode_label.setText("模式：抽象整活" if chaotic else "模式：智能悲壮")
        self.mode_label.setStyleSheet(
            "color: #ff8c00;" if chaotic else "color: #7ee081;"
        )

    def _update_shortcut_labels(self) -> None:
        keys = self.config.hotkeys
        self.force_button.setText(f"强制触发（{keys.force_trigger or '未绑定'}）")
        self.stop_button.setText(f"停止音乐（{keys.stop_music or '未绑定'}）")

    # ------------------------------------------------------------------ #
    # 快捷键
    # ------------------------------------------------------------------ #
    def _install_shortcuts(self) -> None:
        for shortcut in self._shortcuts:
            shortcut.setEnabled(False)
            shortcut.deleteLater()
        self._shortcuts.clear()
        cfg = self.config.hotkeys
        if not cfg.enabled:
            return
        bindings = [
            (cfg.force_trigger, self._on_force_trigger),
            (cfg.stop_music, self._on_stop),
            (cfg.toggle_auto, self._on_toggle_auto_shortcut),
        ]
        for sequence, handler in bindings:
            if not sequence:
                continue
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
            shortcut.activated.connect(handler)
            self._shortcuts.append(shortcut)

    # ------------------------------------------------------------------ #
    # 事件桥
    # ------------------------------------------------------------------ #
    def _connect_bridge(self) -> None:
        self.bridge.state_changed.connect(self._on_state_changed)
        self.bridge.event_started.connect(self._on_event_started)
        self.bridge.event_finished.connect(self._on_event_finished)
        self.bridge.log_message.connect(self._append_log)
        self.bridge.connection_changed.connect(self._on_connection_changed)
        self.bridge.detection_status.connect(self._on_detection_status)
        self.bridge.global_hotkey.connect(self._on_global_hotkey)

    def _on_global_hotkey(self, action: str) -> None:
        """全局热键回调（来自消息线程，经 Qt 信号回到主线程）。"""
        if action == "force_trigger":
            self._append_log("info", "全局热键：强制触发")
            self._on_force_trigger()
        elif action == "stop_music":
            self._append_log("info", "全局热键：停止音乐")
            self._on_stop()
        elif action == "toggle_auto":
            self._on_toggle_auto_shortcut()
        else:  # pragma: no cover - 未知动作
            self._append_log("warning", f"未知的全局热键动作：{action}")

    def _on_detection_status(self, status: Any) -> None:
        kind = getattr(status, "kind", "")
        message = getattr(status, "message", "")
        text = getattr(status, "text", "")
        decision = getattr(status, "decision", None)

        if kind == "listening":
            self.mic_label.setText("麦克风：监听中")
            self.mic_label.setStyleSheet("color: #7ee081;")
        elif kind == "stopped":
            self.mic_label.setText("麦克风：已停止")
            self.mic_label.setStyleSheet("")
        elif kind == "error":
            self.mic_label.setText("麦克风：错误")
            self.mic_label.setStyleSheet("color: #ff6b6b;")

        if text:
            self.transcript_label.setText(f"最近识别：{text}")
        elif kind in ("triggered", "rejected"):
            self.transcript_label.setText("最近识别：（空）")

        if decision is not None:
            self.score_label.setText(f"{decision.combined:.2f}")
            self.reason_label.setText(decision.reason)

        if message:
            level = "error" if kind == "error" else "info"
            if kind in ("triggered", "error"):
                self._append_log(level, message)
            elif kind == "rejected":
                self.statusBar().showMessage(message, 4000)

    def _on_state_changed(self, change: StateChange) -> None:
        self.state_label.setText(f"状态：{STATE_LABELS.get(change.new, change.new.value)}")

    def _on_event_started(self, record: EventRecord) -> None:
        self.event_label.setText(f"{record.event_id} · {record.trigger_source}")
        self.reason_label.setText(record.reason)

    def _on_event_finished(self, record: EventRecord) -> None:
        self.log_widget.add_record(record)
        if record.score is not None:
            self.score_label.setText(f"{record.score:.2f}")
        if record.note:
            level = "error" if record.played_ok is False or record.restore_ok is False else "info"
            self._append_log(level, record.note)

    def _on_connection_changed(self, connected: bool, detail: str) -> None:
        if not self.config.obs.control_enabled:
            self._refresh_obs_label()
            return
        if connected:
            self.obs_label.setText("OBS：已连接")
            self.obs_label.setStyleSheet("color: #7ee081;")
        else:
            self.obs_label.setText("OBS：未连接" + (f"（{detail}）" if detail else ""))
            self.obs_label.setStyleSheet("color: #ff8c00;")

    def _append_log(self, level: str, message: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.statusBar().showMessage(f"[{stamp}] {message}", 8000)

    # ------------------------------------------------------------------ #
    # 交互
    # ------------------------------------------------------------------ #
    def _on_force_trigger(self) -> None:
        result = self.ctx.event_manager.trigger_manual()
        if not result.started:
            self._append_log("warning", result.message)

    def _on_stop(self) -> None:
        if not self.ctx.event_manager.stop():
            self._append_log("info", "当前没有正在播放的事件")

    def _on_skip(self) -> None:
        if not self.ctx.event_manager.skip_candidate():
            self._append_log("info", "当前没有候选事件")

    # -- 曲目切换与添加 ------------------------------------------------------ #
    def _refresh_track_combo(self) -> None:
        cfg = self.config
        combo = self.track_combo
        combo.blockSignals(True)
        combo.clear()
        if not cfg.audio.playlist:
            combo.addItem("（尚未添加音乐）", -1)
            combo.blockSignals(False)
            self._update_track_hint()
            return
        for index, track in enumerate(cfg.audio.playlist):
            path = str(track.get("path", ""))
            title = str(track.get("title") or Path(path).stem or path)
            suffix = "" if Path(path).exists() else "（文件缺失）"
            combo.addItem(f"{index + 1}. {title}{suffix}", index)
        cfg.audio.set_current(cfg.audio.current_index)
        combo.setCurrentIndex(cfg.audio.current_index)
        combo.blockSignals(False)
        self._update_track_hint()

    def _update_track_hint(self) -> None:
        track = self.config.audio.current_track
        if not track:
            self.track_hint.setText(
                "还没有曲目：点「添加音乐…」选择本地音频（WAV / MP3 / FLAC / OGG / AAC / M4A）"
            )
            return
        path = Path(str(track.get("path", "")))
        if not path.exists():
            self.track_hint.setText(f"⚠ 文件不存在：{path}")
            return
        self.track_hint.setText(str(path))

    def _on_track_changed(self, index: int) -> None:
        data = self.track_combo.itemData(index)
        if data is None or int(data) < 0:
            return
        self.config.audio.set_current(int(data))
        self._save_config()
        self._update_track_hint()
        self._append_log("info", f"当前悲壮曲目：{self.config.audio.bgm_title}")
        self._prewarm_current_track()

    def _prewarm_current_track(self) -> None:
        """后台预解码当前曲目：切曲后第一次触发就不用等解码（整首歌解码要 0.5~1 秒）。"""
        track = self.config.audio.current_track
        if not track:
            return
        path = str(track.get("path", ""))
        if not path or not Path(path).exists():
            return
        player = self.ctx.player
        if hasattr(player, "is_prepared") and player.is_prepared(path):
            return
        if getattr(player, "is_active", False):
            return
        run_async(
            self,
            "预热曲目",
            lambda: player.prepare(path),
            on_success=lambda _info: None,
            on_error=lambda msg: self._append_log("warning", f"曲目预热失败：{msg}"),
        )

    def _on_add_music(self) -> None:
        start_dir = self.config.audio.playlist[-1]["path"] if self.config.audio.playlist else ""
        start_dir = str(Path(start_dir).parent) if start_dir else str(Path.home() / "Music")
        paths, _filter = QFileDialog.getOpenFileNames(
            self,
            "添加悲壮音乐（可多选）",
            start_dir,
            "音频文件 (*.wav *.mp3 *.flac *.ogg *.oga *.opus *.aac *.m4a *.wma);;所有文件 (*.*)",
        )
        if not paths:
            return
        added = 0
        last_index = self.config.audio.current_index
        for path in paths:
            before = len(self.config.audio.playlist)
            last_index = self.config.audio.add_track(path, track_title(path))
            if len(self.config.audio.playlist) > before:
                added += 1
        self.config.audio.set_current(last_index)
        self._save_config()
        self._refresh_track_combo()
        if added:
            self._append_log(
                "info", f"已添加 {added} 首曲目，当前：{self.config.audio.bgm_title}"
            )
        else:
            self._append_log("info", "所选曲目已在列表中")

    # -- 麦克风监听 ---------------------------------------------------------- #
    def _on_toggle_listen(self) -> None:
        detection = self.ctx.detection
        if detection is not None and detection.is_running:
            self.listen_button.setEnabled(False)
            self.listen_button.setText("停止中…")
            run_async(
                self,
                "停止监听",
                lambda: self.ctx.stop_listening() or True,
                on_success=lambda _r: self._after_listening_stopped(),
                on_error=lambda msg: self._after_listening_error(msg),
            )
            return

        cfg = self.config
        if not cfg.stt.enabled:
            QMessageBox.information(
                self,
                "麦克风监听",
                "关键词层需要文本，请先在【设置 → 检测】中启用语音识别（faster-whisper）。\n"
                "关闭语音识别时程序不会监听麦克风，避免把噪声当台词。",
            )
            return

        self.listen_button.setEnabled(False)
        self.listen_button.setText("启动中…")
        run_async(
            self,
            "启动监听",
            self._start_listening_task,
            on_success=lambda _r: self._after_listening_started(),
            on_error=lambda msg: self._after_listening_error(msg),
        )

    def _start_listening_task(self) -> str:
        cfg = self.config
        self.ctx.start_listening()
        cfg.mic.enabled = True
        self.ctx.save_config()
        if cfg.stt.enabled and self.ctx.detection is not None:
            # 首次使用会下载/加载本地模型，可能比较慢
            self.ctx.detection.warmup_recognizer()
        return "ok"

    def _after_listening_started(self) -> None:
        self.listen_button.setEnabled(True)
        self.listen_button.setText("停止监听麦克风")
        self.mic_label.setText("麦克风：监听中")
        self.mic_label.setStyleSheet("color: #7ee081;")
        self._append_log("info", "已开始监听麦克风（关闭窗口或点按钮即释放设备）")
        capture = self.ctx.capture
        if capture is not None and getattr(capture, "is_loopback", False):
            name = getattr(capture, "device_name", "")
            self._append_log(
                "warning", f"当前麦克风是回环设备「{name}」：会录到队友语音与游戏声音"
            )
            QMessageBox.warning(
                self,
                "麦克风选择提醒",
                f"当前输入设备是「{name}」——\n"
                "它是系统输出的回环（立体声混音 / WASAPI loopback），\n"
                "所以游戏里队友开麦说的话也会被识别，同样可能触发播放。\n\n"
                "只想监听自己说话：设置 → 检测 → 输入设备，改选真实麦克风（例如「麦克风阵列」）。",
            )

    def _after_listening_stopped(self) -> None:
        self.listen_button.setEnabled(True)
        self.listen_button.setText("开始监听麦克风")
        self.mic_label.setText("麦克风：已停止")
        self.mic_label.setStyleSheet("")
        self.config.mic.enabled = False
        self._save_config()
        self._append_log("info", "已停止监听，麦克风设备已释放")

    def _after_listening_error(self, message: str) -> None:
        self.listen_button.setEnabled(True)
        self.listen_button.setText("开始监听麦克风")
        self.mic_label.setText("麦克风：错误")
        self.mic_label.setStyleSheet("color: #ff6b6b;")
        try:
            self.ctx.stop_listening()
        except Exception:  # pragma: no cover
            pass
        self.config.mic.enabled = False
        self._save_config()
        self._append_log("error", f"麦克风启动失败：{message}")
        QMessageBox.warning(self, "麦克风", f"启动监听失败：\n{message}")

    def _on_toggle_auto_shortcut(self) -> None:
        self.auto_check.setChecked(not self.auto_check.isChecked())

    def _on_auto_toggled(self, checked: bool) -> None:
        self.config.mode.auto_trigger_enabled = checked
        self._save_config()
        self._append_log("info", f"自动触发{'已启用' if checked else '已关闭'}")

    def _on_global_mute_toggled(self, checked: bool) -> None:
        self.config.mode.global_mute = checked
        self._save_config()
        self._append_log("warning", f"全局静音{'已开启' if checked else '已关闭'}")

    def _on_mode_changed(self, _index: int) -> None:
        mode = str(self.mode_combo.currentData() or "serious")
        self.config.mode.current = mode
        self._save_config()
        self._refresh_mode_label()
        self._append_log(
            "info",
            "已切换到抽象整活模式：普通语句也可能触发" if mode == "chaotic" else "已切换到智能悲壮模式",
        )

    # -- 输出方式：OBS 控制 vs 纯本地播歌 ------------------------------------ #
    def _on_output_mode_changed(self, _index: int) -> None:
        enabled = bool(self.output_combo.currentData())
        self.config.obs.control_enabled = enabled
        self._save_config()
        self._refresh_output_hint()
        self._refresh_obs_label()
        if enabled:
            self._append_log(
                "info", "已切到「OBS 控制」：点「连接 / 重新连接 OBS」后即可切断游戏声音"
            )
            return
        try:
            self.ctx.obs_controller.disconnect()
        except Exception:  # pragma: no cover - 断开失败不影响模式切换
            pass
        self._append_log(
            "info", "已切到「仅本地播歌」：不连接 OBS，检测到台词就只放歌"
        )

    def _refresh_output_hint(self) -> None:
        if self.config.obs.control_enabled:
            self.output_hint.setText(
                "触发时静音/压低 OBS 里的游戏音源，BGM 由本程序播放。需要 OBS 正在运行。"
            )
            self.output_hint.setStyleSheet("color: #9aa0c0;")
        else:
            self.output_hint.setText(
                "不开 OBS 也能用：只监听麦克风，命中台词就放歌，适合平时跟人开玩笑。"
                "不碰任何游戏/系统音频，也不需要 OBS。"
            )
            self.output_hint.setStyleSheet("color: #7ee081;")

    def _refresh_obs_label(self) -> None:
        if not self.config.obs.control_enabled:
            self.obs_label.setText("输出：仅本地播歌")
            self.obs_label.setStyleSheet("color: #7ee081;")
            self.connect_button.setEnabled(False)
            self.connect_button.setToolTip("当前是纯本地模式，不需要连接 OBS")
            self.test_button.setText("测试音频（纯本地）")
            return
        self.connect_button.setEnabled(True)
        self.connect_button.setToolTip("")
        self.test_button.setText("测试音频")
        if self.ctx.obs_client.connected:
            self.obs_label.setText("OBS：已连接")
            self.obs_label.setStyleSheet("color: #7ee081;")
        else:
            self.obs_label.setText("OBS：未连接")
            self.obs_label.setStyleSheet("color: #ff8c00;")

    def _on_probability_changed(self, _value: float) -> None:
        probs = self.config.trigger.probabilities
        probs["serious"] = round(self.prob_serious.value() / 100.0, 4)
        probs["low_confidence"] = round(self.prob_low.value() / 100.0, 4)
        probs["chaotic"] = round(self.prob_chaotic.value() / 100.0, 4)
        self._save_config()

    def _on_timing_changed(self, _value: float) -> None:
        self.config.trigger.cooldown_seconds = float(self.cooldown_spin.value())
        self.config.trigger.trigger_threshold = float(self.threshold_spin.value())
        self._save_config()

    def _on_reset_probabilities(self) -> None:
        defaults = self.ctx.config_manager.default_config().trigger
        self._suspend_signals(True)
        self.prob_serious.setValue(defaults.probabilities["serious"] * 100)
        self.prob_low.setValue(defaults.probabilities["low_confidence"] * 100)
        self.prob_chaotic.setValue(defaults.probabilities["chaotic"] * 100)
        self.cooldown_spin.setValue(defaults.cooldown_seconds)
        self.threshold_spin.setValue(defaults.trigger_threshold)
        self._suspend_signals(False)
        self._on_probability_changed(0)
        self._on_timing_changed(0)
        self._append_log("info", "概率与阈值已恢复默认")

    def _on_connect_obs(self) -> None:
        self.connect_button.setEnabled(False)
        self.connect_button.setText("连接中…")
        run_async(
            self,
            "连接 OBS",
            self.ctx.reconnect_obs,
            on_success=self._on_connected,
            on_error=self._on_connect_failed,
        )

    def _on_connected(self, info: str) -> None:
        self.connect_button.setEnabled(True)
        self.connect_button.setText("连接 / 重新连接 OBS")
        self._on_connection_changed(True, "")
        self.obs_label.setToolTip(str(info))
        self._append_log("info", f"已连接 {info}")

    def _on_connect_failed(self, message: str) -> None:
        self.connect_button.setEnabled(True)
        self.connect_button.setText("连接 / 重新连接 OBS")
        self._on_connection_changed(False, message)
        self._append_log("error", f"OBS 连接失败：{message}")

    def _on_test_audio(self) -> None:
        self.test_button.setEnabled(False)
        self.test_button.setText("测试中…")

        def done(result: tuple[bool, str]) -> None:
            ok, message = result
            self.test_button.setEnabled(True)
            self.test_button.setText("测试音频")
            self._append_log("info" if ok else "error", message)
            if not ok:
                QMessageBox.warning(self, "音频测试", message)

        def failed(message: str) -> None:
            self.test_button.setEnabled(True)
            self.test_button.setText("测试音频")
            self._append_log("error", f"音频测试异常：{message}")

        run_async(
            self,
            "音频测试",
            lambda: self.ctx.event_manager.test_audio(),
            on_success=done,
            on_error=failed,
        )

    def _on_reset_audio(self) -> None:
        def done(outcome: Any) -> None:
            message = outcome.summary() if hasattr(outcome, "summary") else str(outcome)
            level = "info" if getattr(outcome, "ok", True) else "error"
            self._append_log(level, "重置音频状态：" + message)
            if not getattr(outcome, "ok", True):
                QMessageBox.warning(self, "重置音频状态", message)

        run_async(self, "重置音频状态", self.ctx.event_manager.reset_audio_state, on_success=done)

    def _on_clear_log(self) -> None:
        answer = QMessageBox.question(
            self,
            "清除日志",
            "确定清空事件日志表格吗？（不会删除日志文件）",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.log_widget.clear()

    def _on_settings(self) -> None:
        dialog = SettingsDialog(
            self.config,
            obs_controller=self.ctx.obs_controller,
            audio_player=self.ctx.player,
            secret_store=self.ctx.secret_store,
            mic_capture=self.ctx.capture,
            parent=self,
        )
        if dialog.exec() != SettingsDialog.DialogCode.Accepted:
            return
        new_config = dialog.result_config()
        self.ctx.apply_config(
            new_config,
            password=dialog.password_value() if dialog.password_changed() else None,
            save_password=dialog.should_save_password() and dialog.password_changed(),
            ai_key=dialog.ai_key_value() if dialog.ai_key_changed() else None,
            save_ai_key=dialog.should_save_ai_key() and dialog.ai_key_changed(),
        )
        self._load_config_into_ui()
        self._install_shortcuts()
        self.log_widget.set_max_entries(new_config.logging.max_entries)
        self._refresh_track_combo()
        self._refresh_output_hint()
        self._refresh_obs_label()
        self._append_log("info", "设置已保存")

    # ------------------------------------------------------------------ #
    # 定时刷新
    # ------------------------------------------------------------------ #
    def _tick(self) -> None:
        player = self.ctx.player
        if player.is_active and player.duration > 0:
            self.progress.setValue(int(player.progress * 1000))
            self.play_label.setText(f"播放：{player.position:.1f}/{player.duration:.1f}s")
            self.play_label.setStyleSheet("color: #ff8c00;")
        else:
            self.progress.setValue(0)
            self.play_label.setText("播放：空闲")
            self.play_label.setStyleSheet("")

        remaining = self.ctx.cooldown.remaining()
        if remaining > 0.05:
            self.cooldown_label.setText(f"冷却中 {remaining:.1f}s")
        else:
            self.cooldown_label.setText("就绪")

        state = self.ctx.state_machine.state
        self.state_label.setText(f"状态：{STATE_LABELS.get(state, state.value)}")
        busy = state in (EventState.PLAYING, EventState.TRIGGERED, EventState.RESTORING)
        self.stop_button.setEnabled(busy or player.is_active)

    def notify_pending_state(self, data: dict[str, Any]) -> None:
        snapshot = data.get("snapshot") or {}
        source = snapshot.get("source", "?")
        muted = snapshot.get("muted")
        message = (
            f"上次运行时程序可能异常退出，音源「{source}」的状态未恢复。\n"
            f"记录的事件前状态：静音={muted}，音量={snapshot.get('volume_mul')}\n\n"
            "现在尝试恢复吗？"
        )
        answer = QMessageBox.question(
            self,
            "发现未恢复的音频状态",
            message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._on_reset_audio()

    # ------------------------------------------------------------------ #
    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 (Qt 命名)
        self._timer.stop()
        try:
            self.ctx.shutdown()
        except Exception:  # pragma: no cover
            log.exception("关闭流程异常")
        event.accept()

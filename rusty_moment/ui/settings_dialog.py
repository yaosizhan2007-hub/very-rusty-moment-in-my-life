"""设置对话框：音频 / OBS / 隐私与日志。

阶段 1 只暴露真正可用的设置项，不留假开关：麦克风、关键词、AI 参数在阶段 2/3
真正实现后再加入本对话框。
"""

from __future__ import annotations

import copy
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QKeySequenceEdit,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..audio.audio_player import AudioFileError, AudioPlayerError
from ..audio.music_library import track_title
from ..config.config_manager import AppConfig
from ..utils.logging_setup import default_log_dir, log_file_path
from .qt_bridge import run_async

log = logging.getLogger(__name__)


def _split_aliases(text: str) -> list[str]:
    """把「别救我,不要管我」拆成列表（容忍中文逗号/顿号/分号）。"""
    raw = (
        (text or "")
        .replace("，", ",")
        .replace("、", ",")
        .replace("；", ",")
        .replace(";", ",")
    )
    out: list[str] = []
    seen: set[str] = set()
    for part in raw.split(","):
        item = part.strip()
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _format_phrases(items: list[dict[str, Any]]) -> str:
    """把词库列表转成「词组|权重|近义说法,近义说法」的多行文本。"""
    lines = []
    for item in items or []:
        phrase = str(item.get("phrase", "")).strip()
        if not phrase:
            continue
        weight = float(item.get("weight", 1.0) or 1.0)
        line = f"{phrase}|{weight:g}"
        aliases = [str(alias).strip() for alias in (item.get("aliases") or []) if str(alias).strip()]
        if aliases:
            line += "|" + ",".join(aliases)
        lines.append(line)
    return "\n".join(lines)


def _parse_phrases(text: str) -> list[dict[str, Any]]:
    """解析「词组|权重|近义说法」多行文本；空行与 # 注释忽略，权重越界自动收敛。"""
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        separator = "|" if "|" in line else ("｜" if "｜" in line else "")
        if separator:
            parts = line.split(separator, 2)
        else:
            parts = [line]
        phrase = parts[0].strip()
        weight_text = parts[1] if len(parts) > 1 else "1.0"
        alias_text = parts[2] if len(parts) > 2 else ""
        if not phrase or phrase in seen:
            continue
        try:
            weight = float(weight_text.strip() or 1.0)
        except ValueError:
            weight = 1.0
        entry: dict[str, Any] = {"phrase": phrase, "weight": max(0.0, min(2.0, weight))}
        aliases = [alias for alias in _split_aliases(alias_text) if alias != phrase]
        if aliases:
            entry["aliases"] = aliases
        seen.add(phrase)
        result.append(entry)
    return result


class SettingsDialog(QDialog):
    def __init__(
        self,
        config: AppConfig,
        *,
        obs_controller: Any = None,
        audio_player: Any = None,
        secret_store: Any = None,
        password_key: str = "obs_password",
        mic_capture: Any = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Rusty Moment 设置")
        self.resize(680, 620)
        self._cfg = copy.deepcopy(config)
        self._tracks: list[dict[str, Any]] = [dict(track) for track in config.audio.playlist]
        self._current_index = int(config.audio.current_index)
        self._obs = obs_controller
        self._player = audio_player
        self._secrets = secret_store
        self._mic_capture = mic_capture
        self._password_key = password_key
        # AI 的 key 用固定条目名（与 app.AI_API_KEY 保持一致）
        self._saved_ai_key = ""
        if secret_store is not None and getattr(secret_store, "available", False):
            try:
                self._saved_ai_key = secret_store.get_secret("ai_api_key") or ""
            except Exception:  # pragma: no cover
                self._saved_ai_key = ""
        self._password_value = ""
        self._new_password_set = False

        tabs = QTabWidget(self)
        tabs.addTab(self._build_audio_tab(), "音频")
        tabs.addTab(self._build_obs_tab(), "OBS")
        tabs.addTab(self._build_detection_tab(), "检测（阶段 2）")
        tabs.addTab(self._build_privacy_tab(), "隐私与日志")

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(tabs)
        layout.addWidget(buttons)

    # ------------------------------------------------------------------ #
    # 音频页
    # ------------------------------------------------------------------ #
    def _build_audio_tab(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)

        box = QGroupBox("悲壮 BGM 曲目（默认从系统「音乐」目录收三首）", page)
        box_layout = QVBoxLayout(box)
        self.track_list = QListWidget(box)
        self.track_list.setMinimumHeight(140)
        self.track_list.currentRowChanged.connect(self._on_track_row_changed)
        self.track_list.itemDoubleClicked.connect(lambda _item: self._toggle_preview())
        box_layout.addWidget(self.track_list)

        track_buttons = QHBoxLayout()
        add_button = QPushButton("添加音乐…", box)
        add_button.clicked.connect(self._add_tracks)
        remove_button = QPushButton("移除", box)
        remove_button.clicked.connect(self._remove_track)
        up_button = QPushButton("上移", box)
        up_button.clicked.connect(lambda: self._move_track(-1))
        down_button = QPushButton("下移", box)
        down_button.clicked.connect(lambda: self._move_track(1))
        for button in (add_button, remove_button, up_button, down_button):
            track_buttons.addWidget(button)
        track_buttons.addStretch(1)
        box_layout.addLayout(track_buttons)

        info_row = QHBoxLayout()
        self.duration_label = QLabel("—", box)
        self.duration_label.setWordWrap(True)
        self.preview_button = QPushButton("试听选中曲目", box)
        self.preview_button.clicked.connect(self._toggle_preview)
        stop_button = QPushButton("停止", box)
        stop_button.clicked.connect(self._stop_preview)
        info_row.addWidget(self.duration_label, 1)
        info_row.addWidget(self.preview_button)
        info_row.addWidget(stop_button)
        box_layout.addLayout(info_row)

        track_hint = QLabel(
            "支持 WAV / MP3 / FLAC / OGG / AAC / M4A；列表里选中的那首就是触发时播放的曲子。"
            "双击曲目可试听。",
            box,
        )
        track_hint.setWordWrap(True)
        track_hint.setStyleSheet("color: #9aa0c0;")
        box_layout.addWidget(track_hint)

        form = QFormLayout()
        box_layout.addLayout(form)

        self.volume_spin = QDoubleSpinBox(box)
        self.volume_spin.setRange(0, 100)
        self.volume_spin.setSuffix(" %")
        self.volume_spin.setValue(self._cfg.audio.volume * 100)
        form.addRow("播放音量", self.volume_spin)

        self.device_combo = QComboBox(box)
        self.device_combo.addItem("系统默认输出", "")
        for index, name in self._output_devices():
            self.device_combo.addItem(f"{name}  (#{index})", str(index))
        current = self._cfg.audio.output_device
        idx = self.device_combo.findData(current)
        self.device_combo.setCurrentIndex(idx if idx >= 0 else 0)
        form.addRow("输出设备", self.device_combo)

        self.from_start_check = QCheckBox("每次从开头播放", box)
        self.from_start_check.setChecked(self._cfg.audio.play_from_start)
        form.addRow("", self.from_start_check)

        self.loop_check = QCheckBox("允许循环播放（不推荐：会一直压低游戏声）", box)
        self.loop_check.setChecked(self._cfg.audio.allow_repeat)
        form.addRow("", self.loop_check)

        layout.addWidget(box)
        layout.addStretch(1)
        self._refresh_track_list()
        return page

    def _percent_spin(self, parent: QWidget) -> QDoubleSpinBox:
        """百分比输入框（0~100，保存时除以 100）。"""
        spin = QDoubleSpinBox(parent)
        spin.setRange(0, 100)
        spin.setDecimals(0)
        spin.setSuffix(" %")
        return spin

    def _output_devices(self) -> list[tuple[int, str]]:
        if self._player is None:
            return []
        try:
            return self._player.list_output_devices()
        except Exception as exc:
            log.warning("枚举输出设备失败：%s", exc)
            return []

    def _add_tracks(self) -> None:
        start_dir = (
            str(Path(self._tracks[-1]["path"]).parent)
            if self._tracks
            else str(Path.home() / "Music")
        )
        paths, _filter = QFileDialog.getOpenFileNames(
            self,
            "添加悲壮音乐（可多选）",
            start_dir,
            "音频文件 (*.wav *.mp3 *.flac *.ogg *.oga *.opus *.aac *.m4a *.wma);;所有文件 (*.*)",
        )
        if not paths:
            return
        existing = {str(track.get("path", "")) for track in self._tracks}
        added = 0
        for path in paths:
            if path in existing:
                continue
            self._tracks.append({"path": path, "title": track_title(path)})
            existing.add(path)
            added += 1
        if added:
            self._current_index = len(self._tracks) - 1
        self._refresh_track_list()
        self._refresh_duration()

    def _remove_track(self) -> None:
        row = self.track_list.currentRow()
        if not (0 <= row < len(self._tracks)):
            return
        removed = self._tracks.pop(row)
        if self._current_index >= len(self._tracks):
            self._current_index = max(0, len(self._tracks) - 1)
        self._refresh_track_list()
        self.duration_label.setText(f"已移除：{removed.get('title', '')}")

    def _move_track(self, delta: int) -> None:
        row = self.track_list.currentRow()
        if not (0 <= row < len(self._tracks)):
            return
        target = max(0, min(len(self._tracks) - 1, row + delta))
        if target == row:
            return
        track = self._tracks.pop(row)
        self._tracks.insert(target, track)
        if self._current_index == row:
            self._current_index = target
        elif row < self._current_index <= target:
            self._current_index -= 1
        elif target <= self._current_index < row:
            self._current_index += 1
        self._refresh_track_list()
        self.track_list.setCurrentRow(target)

    def _refresh_track_list(self) -> None:
        self.track_list.blockSignals(True)
        try:
            self.track_list.clear()
            for index, track in enumerate(self._tracks):
                path = str(track.get("path", ""))
                title = str(track.get("title") or Path(path).stem or path)
                marker = "" if Path(path).exists() else "  ⚠ 文件缺失"
                prefix = "▶ " if index == self._current_index else "   "
                self.track_list.addItem(f"{prefix}{index + 1}. {title}{marker}")
            row = (
                min(max(0, self._current_index), len(self._tracks) - 1) if self._tracks else -1
            )
            if row >= 0:
                self.track_list.setCurrentRow(row)
                self._current_index = row
        finally:
            self.track_list.blockSignals(False)
        self._refresh_duration()

    def _on_track_row_changed(self, row: int) -> None:
        if 0 <= row < len(self._tracks):
            self._current_index = row
            self._refresh_track_list()
            self.track_list.setCurrentRow(row)

    def _current_track(self) -> dict[str, Any] | None:
        if not self._tracks:
            return None
        index = min(max(0, self._current_index), len(self._tracks) - 1)
        return self._tracks[index]

    def _refresh_duration(self) -> None:
        track = self._current_track()
        if not track:
            self.duration_label.setText("—（还没有曲目，点「添加音乐…」）")
            return
        path = str(track.get("path", ""))
        if not Path(path).exists():
            self.duration_label.setText(f"⚠ 文件不存在：{path}")
            return
        if self._player is None:
            self.duration_label.setText(str(path))
            return
        try:
            info = self._player.load(path)
            self.duration_label.setText(
                f"{info.duration_s:.1f} 秒 · {info.samplerate} Hz · "
                f"{info.channels} 声道 · {info.format}"
            )
        except (AudioFileError, AudioPlayerError) as exc:
            self.duration_label.setText(f"⚠ {exc}")

    def _toggle_preview(self) -> None:
        if self._player is None:
            return
        if self._player.is_active:
            self._stop_preview()
            return
        track = self._current_track()
        path = str(track.get("path", "")) if track else ""
        if not path:
            QMessageBox.warning(self, "试听", "请先在列表里选择一首曲目")
            return
        try:
            self._player.play(
                path,
                volume=self.volume_spin.value() / 100.0,
                from_start=True,
                loop=False,
                device=self.device_combo.currentData() or "",
            )
        except (AudioFileError, AudioPlayerError) as exc:
            QMessageBox.critical(self, "试听失败", str(exc))
            return
        self.preview_button.setText("正在试听…")
        run_async(
            self,
            "试听",
            lambda: self._player.wait_until_inactive(timeout=3600),
            on_success=lambda _res: self.preview_button.setText("试听"),
            on_error=lambda _msg: self.preview_button.setText("试听"),
        )

    def _stop_preview(self) -> None:
        if self._player is not None:
            self._player.stop()
        self.preview_button.setText("试听")

    # ------------------------------------------------------------------ #
    # OBS 页
    # ------------------------------------------------------------------ #
    def _build_obs_tab(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)

        mode_box = QGroupBox("输出方式", page)
        mode_layout = QVBoxLayout(mode_box)
        self.output_mode_combo = QComboBox(mode_box)
        self.output_mode_combo.addItem("OBS 控制（切断/压低游戏声）", True)
        self.output_mode_combo.addItem("仅本地播歌（不连 OBS，平时整活用）", False)
        idx = self.output_mode_combo.findData(bool(self._cfg.obs.control_enabled))
        self.output_mode_combo.setCurrentIndex(max(0, idx))
        self.output_mode_combo.currentIndexChanged.connect(self._on_output_mode_preview)
        mode_layout.addWidget(self.output_mode_combo)
        self.output_mode_hint = QLabel("", mode_box)
        self.output_mode_hint.setWordWrap(True)
        mode_layout.addWidget(self.output_mode_hint)
        self._on_output_mode_preview()
        layout.addWidget(mode_box)

        conn_box = QGroupBox("连接（obs-websocket 5.x）", page)
        form = QFormLayout(conn_box)
        self.host_edit = QLineEdit(self._cfg.obs.host, conn_box)
        form.addRow("地址", self.host_edit)
        self.port_spin = QSpinBox(conn_box)
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(self._cfg.obs.port)
        form.addRow("端口", self.port_spin)

        self.password_edit = QLineEdit(conn_box)
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_edit.setPlaceholderText("OBS → 工具 → WebSocket 服务器设置 中的密码")
        form.addRow("密码", self.password_edit)
        if self._secrets is not None and getattr(self._secrets, "available", False):
            saved = self._secrets.get_secret(self._password_key)
            if saved:
                self._password_value = saved
                self.password_edit.setText(saved)
                self.password_edit.setPlaceholderText("已保存（Windows 加密存储）")
        self.save_password_check = QCheckBox("保存密码（Windows DPAPI 加密，仅本机本用户可解）", conn_box)
        self.save_password_check.setChecked(bool(self._password_value))
        if self._secrets is not None and not getattr(self._secrets, "available", False):
            self.save_password_check.setEnabled(False)
            self.save_password_check.setText("无法保存密码：缺少 pywin32，请每次启动时手动输入")
        form.addRow("", self.save_password_check)

        self.autoreconnect_check = QCheckBox("断开后自动重连", conn_box)
        self.autoreconnect_check.setChecked(self._cfg.obs.auto_reconnect)
        form.addRow("", self.autoreconnect_check)
        self.reconnect_spin = QDoubleSpinBox(conn_box)
        self.reconnect_spin.setRange(1.0, 120.0)
        self.reconnect_spin.setSuffix(" 秒")
        self.reconnect_spin.setValue(self._cfg.obs.reconnect_interval_s)
        form.addRow("重连间隔", self.reconnect_spin)

        test_row = QHBoxLayout()
        test_button = QPushButton("测试连接", conn_box)
        test_button.clicked.connect(self._test_connection)
        self.conn_status = QLabel("未测试", conn_box)
        test_row.addWidget(test_button)
        test_row.addWidget(self.conn_status, 1)
        form.addRow("连接测试", test_row)
        layout.addWidget(conn_box)

        source_box = QGroupBox("音源选择", page)
        source_form = QFormLayout(source_box)
        self.game_source_combo = QComboBox(source_box)
        self.game_source_combo.setEditable(True)
        self.game_source_combo.setCurrentText(self._cfg.obs.game_source)
        refresh = QPushButton("刷新音源列表", source_box)
        refresh.clicked.connect(self._refresh_sources)
        game_row = QHBoxLayout()
        game_row.addWidget(self.game_source_combo, 1)
        game_row.addWidget(refresh)
        source_form.addRow("游戏音源", game_row)

        self.bgm_source_combo = QComboBox(source_box)
        self.bgm_source_combo.setEditable(True)
        self.bgm_source_combo.setCurrentText(self._cfg.obs.bgm_source)
        source_form.addRow("BGM 音源（可选）", self.bgm_source_combo)

        self.mic_source_combo = QComboBox(source_box)
        self.mic_source_combo.setEditable(True)
        self.mic_source_combo.setCurrentText(self._cfg.obs.mic_source)
        source_form.addRow("麦克风参考音源（可选）", self.mic_source_combo)

        self.strategy_combo = QComboBox(source_box)
        self.strategy_combo.addItem("立即静音", "mute")
        self.strategy_combo.addItem("压低音量", "duck")
        idx = self.strategy_combo.findData(self._cfg.obs.control_strategy)
        self.strategy_combo.setCurrentIndex(max(0, idx))
        source_form.addRow("控制策略", self.strategy_combo)

        self.duck_spin = QDoubleSpinBox(source_box)
        self.duck_spin.setRange(0, 100)
        self.duck_spin.setSuffix(" %")
        self.duck_spin.setValue(self._cfg.obs.duck_volume * 100)
        source_form.addRow("压低音量到", self.duck_spin)

        self.restore_combo = QComboBox(source_box)
        self.restore_combo.addItem("恢复事件前的原始状态（推荐）", "restore_original")
        self.restore_combo.addItem("恢复时强制取消静音（有覆盖风险）", "force_unmute")
        idx = self.restore_combo.findData(self._cfg.obs.restore_strategy)
        self.restore_combo.setCurrentIndex(max(0, idx))
        source_form.addRow("恢复策略", self.restore_combo)

        hint = QLabel(
            "⚠ 本版本仅控制 OBS 内部音源，不会直接切断 Windows 中游戏自身的声音。\n"
            "请把游戏声音作为 OBS 的独立音源（例如「桌面音频」或「应用音频采集」）后再控制。",
            source_box,
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #ffc857;")
        source_form.addRow(hint)
        layout.addWidget(source_box)
        layout.addStretch(1)
        return page

    def _on_output_mode_preview(self) -> None:
        """输出方式下拉的即时说明（保存后由 MainWindow 真正切换）。"""
        enabled = True
        combo = getattr(self, "output_mode_combo", None)
        if combo is not None:
            enabled = bool(combo.currentData())
        hint = getattr(self, "output_mode_hint", None)
        if hint is None:
            return
        if enabled:
            hint.setText("触发时静音/压低 OBS 里的游戏音源，BGM 由本程序播放。需要 OBS 正在运行。")
            hint.setStyleSheet("color: #9aa0c0;")
        else:
            hint.setText(
                "纯本地模式：只监听麦克风并播放音乐，不需要 OBS，也不会碰任何游戏/系统音频。"
                "下面的 OBS 设置会被忽略。"
            )
            hint.setStyleSheet("color: #7ee081;")

    def _test_connection(self) -> None:
        if self._obs is None:
            self.conn_status.setText("没有可用的 OBS 控制器")
            return
        self.conn_status.setText("连接中…")
        host, port = self.host_edit.text().strip(), self.port_spin.value()
        password = self.password_edit.text()
        saved = ""
        if self._secrets is not None and getattr(self._secrets, "available", False):
            saved = self._secrets.get_secret(self._password_key) or ""
        effective_password = password or saved

        def task() -> str:
            # 用一次性客户端测试，避免把未保存的地址/密码遗留给正在运行的连接
            from ..obs.obs_client import OBSClient
            from ..obs.obs_controller import OBSController

            probe = OBSController(
                OBSClient(
                    host=host,
                    port=port,
                    password_provider=lambda: effective_password,
                    timeout=5.0,
                )
            )
            try:
                return probe.connect()
            finally:
                probe.disconnect()

        run_async(
            self,
            "连接测试",
            task,
            on_success=lambda info: self.conn_status.setText(f"✅ {info}"),
            on_error=lambda msg: self.conn_status.setText(f"❌ {msg}"),
        )

    def _refresh_sources(self) -> None:
        if self._obs is None:
            return

        def task() -> list[str]:
            return self._obs.list_audio_sources(force_refresh=True)

        def apply(sources: list[str]) -> None:
            for combo in (self.game_source_combo, self.bgm_source_combo, self.mic_source_combo):
                current = combo.currentText()
                combo.clear()
                combo.addItems(sources)
                combo.setCurrentText(current)

        run_async(self, "刷新音源", task, on_success=apply, on_error=lambda _m: None)

    # ------------------------------------------------------------------ #
    # 检测（阶段 2）
    # ------------------------------------------------------------------ #
    def _build_detection_tab(self) -> QWidget:
        from PySide6.QtWidgets import QPlainTextEdit, QScrollArea

        page = QWidget(self)
        outer = QVBoxLayout(page)
        scroll = QScrollArea(page)
        scroll.setWidgetResizable(True)
        inner = QWidget(scroll)
        layout = QVBoxLayout(inner)

        # --- 麦克风 ---
        mic_box = QGroupBox("麦克风", inner)
        mic_form = QFormLayout(mic_box)
        self.mic_device_combo = QComboBox(mic_box)
        self.mic_device_combo.setEditable(False)
        self._fill_mic_devices()
        refresh = QPushButton("刷新设备", mic_box)
        refresh.clicked.connect(self._fill_mic_devices)
        mic_row = QHBoxLayout()
        mic_row.addWidget(self.mic_device_combo, 1)
        mic_row.addWidget(refresh)
        mic_form.addRow("输入设备", mic_row)

        self.mic_samplerate_combo = QComboBox(mic_box)
        for rate in (8000, 16000, 22050, 44100, 48000):
            self.mic_samplerate_combo.addItem(f"{rate} Hz", rate)
        idx = self.mic_samplerate_combo.findData(self._cfg.mic.sample_rate)
        self.mic_samplerate_combo.setCurrentIndex(max(0, idx))
        mic_form.addRow("采样率", self.mic_samplerate_combo)

        self.vad_sensitivity_spin = QDoubleSpinBox(mic_box)
        self.vad_sensitivity_spin.setRange(0.0, 1.0)
        self.vad_sensitivity_spin.setSingleStep(0.05)
        self.vad_sensitivity_spin.setDecimals(2)
        self.vad_sensitivity_spin.setValue(self._cfg.mic.vad_sensitivity)
        mic_form.addRow("VAD 灵敏度", self.vad_sensitivity_spin)

        mic_hint = QLabel(
            "灵敏度越高越容易把轻声判成语音；背景噪声大时请调低。"
            "关闭监听后程序会立即释放麦克风设备。",
            mic_box,
        )
        mic_hint.setWordWrap(True)
        mic_hint.setStyleSheet("color: #9aa0c0;")
        mic_form.addRow(mic_hint)
        layout.addWidget(mic_box)

        # --- 语音识别 ---
        stt_box = QGroupBox("语音识别（本地 faster-whisper）", inner)
        stt_form = QFormLayout(stt_box)
        self.stt_enabled_check = QCheckBox("启用本地语音识别（首次使用会下载模型）", stt_box)
        self.stt_enabled_check.setChecked(self._cfg.stt.enabled)
        stt_form.addRow(self.stt_enabled_check)

        self.stt_model_combo = QComboBox(stt_box)
        for size, label in (
            ("tiny", "tiny（最快，识别粗糙）"),
            ("base", "base（较快）"),
            ("small", "small（推荐：速度与准确率平衡）"),
            ("medium", "medium（更准，更慢）"),
        ):
            self.stt_model_combo.addItem(label, size)
        idx = self.stt_model_combo.findData(self._cfg.stt.model_size)
        self.stt_model_combo.setCurrentIndex(max(0, idx if idx >= 0 else 2))
        stt_form.addRow("模型", self.stt_model_combo)

        self.stt_compute_combo = QComboBox(stt_box)
        for value, label in (
            ("int8", "int8（CPU 推荐）"),
            ("float32", "float32（更准更慢）"),
            ("int8_float16", "int8_float16（GPU）"),
        ):
            self.stt_compute_combo.addItem(label, value)
        idx = self.stt_compute_combo.findData(self._cfg.stt.compute_type)
        self.stt_compute_combo.setCurrentIndex(max(0, idx))
        stt_form.addRow("计算类型", self.stt_compute_combo)

        self.stt_language_edit = QLineEdit(self._cfg.stt.language, stt_box)
        self.stt_language_edit.setPlaceholderText("zh / en / auto")
        stt_form.addRow("语言", self.stt_language_edit)

        self.stt_model_dir_edit = QLineEdit(self._cfg.stt.model_dir, stt_box)
        self.stt_model_dir_edit.setPlaceholderText("留空则下载到默认缓存目录")
        browse_model = QPushButton("选择…", stt_box)
        browse_model.clicked.connect(self._pick_model_dir)
        model_row = QHBoxLayout()
        model_row.addWidget(self.stt_model_dir_edit, 1)
        model_row.addWidget(browse_model)
        stt_form.addRow("模型目录", model_row)

        stt_hint = QLabel(
            "语音只在本机转成文字，默认不保存音频、不保存转录文本；"
            "日志只记录字数与耗时。",
            stt_box,
        )
        stt_hint.setWordWrap(True)
        stt_hint.setStyleSheet("color: #9aa0c0;")
        stt_form.addRow(stt_hint)
        layout.addWidget(stt_box)

        # --- 关键词 ---
        kw_box = QGroupBox("关键词层", inner)
        kw_layout = QVBoxLayout(kw_box)
        self.keywords_enabled_check = QCheckBox("启用关键词层", kw_box)
        self.keywords_enabled_check.setChecked(self._cfg.keywords.enabled)
        kw_layout.addWidget(self.keywords_enabled_check)
        kw_layout.addWidget(QLabel("正向触发词（每行一条：词组|权重|近义说法，后两段可省略）", kw_box))
        self.keywords_positive_edit = QPlainTextEdit(_format_phrases(self._cfg.keywords.positives), kw_box)
        self.keywords_positive_edit.setMinimumHeight(110)
        kw_layout.addWidget(self.keywords_positive_edit)
        kw_layout.addWidget(
            QLabel("反向排除词（命中会按权重打折，不会强制否决）", kw_box)
        )
        self.keywords_exclusion_edit = QPlainTextEdit(_format_phrases(self._cfg.keywords.exclusions), kw_box)
        self.keywords_exclusion_edit.setMinimumHeight(80)
        kw_layout.addWidget(self.keywords_exclusion_edit)
        self.keywords_min_hits_spin = QSpinBox(kw_box)
        self.keywords_min_hits_spin.setRange(1, 10)
        self.keywords_min_hits_spin.setValue(self._cfg.keywords.min_hits)
        min_row = QHBoxLayout()
        min_row.addWidget(QLabel("最少命中词数", kw_box))
        min_row.addWidget(self.keywords_min_hits_spin)
        min_row.addStretch(1)
        kw_layout.addLayout(min_row)
        layout.addWidget(kw_box)

        # --- 规则与权重 ---
        rule_box = QGroupBox("规则层与合成权重", inner)
        rule_form = QFormLayout(rule_box)
        self.rules_enabled_check = QCheckBox("启用声学规则层（时长/停顿/音量变化）", rule_box)
        self.rules_enabled_check.setChecked(self._cfg.rules.enabled)
        rule_form.addRow(self.rules_enabled_check)

        weights = self._cfg.trigger.weights
        self.weight_ai_spin = self._percent_spin(rule_box)
        self.weight_ai_spin.setValue(weights.get("ai", 0.55) * 100)
        rule_form.addRow("AI 层权重", self.weight_ai_spin)
        self.weight_keyword_spin = self._percent_spin(rule_box)
        self.weight_keyword_spin.setValue(weights.get("keyword", 0.3) * 100)
        rule_form.addRow("关键词层权重", self.weight_keyword_spin)
        self.weight_rule_spin = self._percent_spin(rule_box)
        self.weight_rule_spin.setValue(weights.get("rule", 0.15) * 100)
        rule_form.addRow("规则层权重", self.weight_rule_spin)

        self.min_voice_spin = QDoubleSpinBox(rule_box)
        self.min_voice_spin.setRange(0.0, 10.0)
        self.min_voice_spin.setSingleStep(0.1)
        self.min_voice_spin.setSuffix(" 秒")
        self.min_voice_spin.setValue(self._cfg.trigger.min_voice_seconds)
        rule_form.addRow("最短语音时长", self.min_voice_spin)

        self.dedupe_spin = QDoubleSpinBox(rule_box)
        self.dedupe_spin.setRange(0.0, 120.0)
        self.dedupe_spin.setSuffix(" 秒")
        self.dedupe_spin.setValue(self._cfg.trigger.dedupe_window_seconds)
        rule_form.addRow("同句去重窗口", self.dedupe_spin)

        weight_hint = QLabel(
            "权重会自动归一化（只按实际启用的层重新分配），不会把未归一化的分数直接相加。",
            rule_box,
        )
        weight_hint.setWordWrap(True)
        weight_hint.setStyleSheet("color: #9aa0c0;")
        rule_form.addRow(weight_hint)
        layout.addWidget(rule_box)

        # --- AI 语义层 ---
        ai_box = QGroupBox("AI 语义层（可选；关闭时只用关键词 + 规则）", inner)
        ai_form = QFormLayout(ai_box)
        self.ai_enabled_check = QCheckBox("启用 AI 语义分析", ai_box)
        self.ai_enabled_check.setChecked(self._cfg.ai.enabled)
        ai_form.addRow(self.ai_enabled_check)

        self.ai_backend_combo = QComboBox(ai_box)
        self.ai_backend_combo.addItem("不使用", "none")
        self.ai_backend_combo.addItem("本地模型（Ollama / OpenAI 兼容）", "local_llm")
        self.ai_backend_combo.addItem("云端 API（OpenAI 兼容）", "openai_compatible")
        idx = self.ai_backend_combo.findData(self._cfg.ai.backend)
        self.ai_backend_combo.setCurrentIndex(max(0, idx))
        ai_form.addRow("后端", self.ai_backend_combo)

        self.ai_base_url_edit = QLineEdit(self._cfg.ai.base_url, ai_box)
        self.ai_base_url_edit.setPlaceholderText(
            "本地默认 http://127.0.0.1:11434/v1 ；云端填 https://api.openai.com/v1"
        )
        ai_form.addRow("服务地址", self.ai_base_url_edit)

        self.ai_model_edit = QLineEdit(self._cfg.ai.model, ai_box)
        self.ai_model_edit.setPlaceholderText("例如 qwen3:8b / gpt-4o-mini")
        ai_form.addRow("模型", self.ai_model_edit)

        self.ai_key_edit = QLineEdit(ai_box)
        self.ai_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        if self._saved_ai_key:
            self.ai_key_edit.setText(self._saved_ai_key)
            self.ai_key_edit.setPlaceholderText("已保存（Windows 加密存储）")
        else:
            self.ai_key_edit.setPlaceholderText("本地模型可留空；云端 API Key")
        ai_form.addRow("API Key", self.ai_key_edit)

        self.save_ai_key_check = QCheckBox("保存 API Key（DPAPI 加密，仅本机本用户可解）", ai_box)
        self.save_ai_key_check.setChecked(bool(self._saved_ai_key))
        ai_form.addRow("", self.save_ai_key_check)

        self.ai_timeout_spin = QDoubleSpinBox(ai_box)
        self.ai_timeout_spin.setRange(0.5, 30.0)
        self.ai_timeout_spin.setSingleStep(0.5)
        self.ai_timeout_spin.setSuffix(" 秒")
        self.ai_timeout_spin.setValue(self._cfg.ai.timeout_seconds)
        ai_form.addRow("超时", self.ai_timeout_spin)

        self.ai_context_spin = QSpinBox(ai_box)
        self.ai_context_spin.setRange(0, 10)
        self.ai_context_spin.setValue(self._cfg.ai.max_context)
        ai_form.addRow("上下文句数", self.ai_context_spin)

        ai_test_row = QHBoxLayout()
        ai_test_button = QPushButton("测试 AI", ai_box)
        ai_test_button.clicked.connect(self._test_ai)
        self.ai_status_label = QLabel("未测试", ai_box)
        self.ai_status_label.setWordWrap(True)
        ai_test_row.addWidget(ai_test_button)
        ai_test_row.addWidget(self.ai_status_label, 1)
        ai_form.addRow("连接测试", ai_test_row)

        ai_hint = QLabel(
            "云端后端必须先在「隐私与日志」里勾选同意，且只会发送识别出的文本（不含音频）。"
            "AI 超时或返回非法数据时程序会自动退回关键词+规则。",
            ai_box,
        )
        ai_hint.setWordWrap(True)
        ai_hint.setStyleSheet("color: #9aa0c0;")
        ai_form.addRow(ai_hint)
        layout.addWidget(ai_box)

        # --- 快捷键 ---
        hotkey_box = QGroupBox("快捷键", inner)
        hotkey_form = QFormLayout(hotkey_box)
        self.hotkeys_enabled_check = QCheckBox("启用快捷键（窗口激活时生效）", hotkey_box)
        self.hotkeys_enabled_check.setChecked(self._cfg.hotkeys.enabled)
        hotkey_form.addRow(self.hotkeys_enabled_check)

        self.hotkey_force_edit = QKeySequenceEdit(QKeySequence(self._cfg.hotkeys.force_trigger), hotkey_box)
        hotkey_form.addRow("强制触发", self.hotkey_force_edit)
        self.hotkey_stop_edit = QKeySequenceEdit(QKeySequence(self._cfg.hotkeys.stop_music), hotkey_box)
        hotkey_form.addRow("停止音乐", self.hotkey_stop_edit)
        self.hotkey_auto_edit = QKeySequenceEdit(QKeySequence(self._cfg.hotkeys.toggle_auto), hotkey_box)
        hotkey_form.addRow("切换自动触发", self.hotkey_auto_edit)

        self.hotkeys_global_check = QCheckBox(
            "注册为全局热键（游戏窗口激活时也能用）", hotkey_box
        )
        self.hotkeys_global_check.setChecked(self._cfg.hotkeys.global_enabled)
        hotkey_form.addRow(self.hotkeys_global_check)
        global_hint = QLabel(
            "⚠ 全局热键会被本程序独占：游戏里按同一个键将不再响应游戏。"
            "只有确实需要「在游戏里一键触发」时才打开，并避开游戏常用按键。",
            hotkey_box,
        )
        global_hint.setWordWrap(True)
        global_hint.setStyleSheet("color: #ffc857;")
        hotkey_form.addRow(global_hint)
        layout.addWidget(hotkey_box)

        layout.addStretch(1)
        scroll.setWidget(inner)
        outer.addWidget(scroll)
        return page

    def _test_ai(self) -> None:
        from ..detection.ai_backends import build_analyzer

        cfg = self.result_config()
        key = self.ai_key_edit.text() or self._saved_ai_key
        analyzer = build_analyzer(cfg.ai, lambda: key)
        if not getattr(analyzer, "available", False):
            reason = getattr(analyzer, "unavailable_reason", "") or "AI 未启用"
            self.ai_status_label.setText(f"❌ {reason}")
            return
        if cfg.ai.backend == "openai_compatible" and not self.consent_check.isChecked():
            self.ai_status_label.setText("❌ 请先在「隐私与日志」里同意发送语音文本")
            return
        self.ai_status_label.setText("分析中…")
        sample = "兄弟，别管我，你们快走，我来拖住他们。"
        run_async(
            self,
            "AI 测试",
            lambda: analyzer.analyze(sample),
            on_success=lambda scores: self.ai_status_label.setText(f"✅ {scores.describe()}"),
            on_error=lambda message: self.ai_status_label.setText(f"❌ {message}"),
        )

    def _fill_mic_devices(self) -> None:
        current = self.mic_device_combo.currentData() if hasattr(self, "mic_device_combo") else ""
        if not hasattr(self, "mic_device_combo"):
            return
        self.mic_device_combo.clear()
        self.mic_device_combo.addItem("系统默认输入", "")
        devices = []
        if self._mic_capture is not None:
            try:
                devices = self._mic_capture.list_input_devices()
            except Exception:  # pragma: no cover - 设备枚举失败不应阻塞设置
                devices = []
        for device in devices:
            suffix = "（⚠ 会录到系统声音，含队友语音）" if device.is_loopback else ""
            self.mic_device_combo.addItem(f"{device.name}  (#{device.index}){suffix}", str(device.index))
        saved = current or self._cfg.mic.device
        idx = self.mic_device_combo.findData(str(saved)) if saved else 0
        self.mic_device_combo.setCurrentIndex(max(0, idx))

    def _pick_model_dir(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "选择语音模型目录", self.stt_model_dir_edit.text())
        if directory:
            self.stt_model_dir_edit.setText(directory)

    # ------------------------------------------------------------------ #
    # 隐私与日志
    # ------------------------------------------------------------------ #
    def _build_privacy_tab(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)

        privacy_box = QGroupBox("麦克风与隐私", page)
        pv = QVBoxLayout(privacy_box)
        text = QLabel(
            "· 麦克风必须由你手动启用；关闭监听后程序不会继续采集。\n"
            "· 默认不保存原始录音，也不上传原始音频。\n"
            "· 本地语音转文字只在内存里做判断，默认不落盘完整转录。\n"
            "· 若启用云端 AI 分析，只有识别出的文本（不含音频）会发送到你配置的服务。",
            privacy_box,
        )
        text.setWordWrap(True)
        pv.addWidget(text)

        self.consent_check = QCheckBox("我了解并同意：启用云端 AI 分析时，语音文本会发送到第三方服务", privacy_box)
        self.consent_check.setChecked(self._cfg.privacy.cloud_consent)
        pv.addWidget(self.consent_check)

        self.store_transcript_check = QCheckBox("在本地保存完整语音转录文本（默认关闭）", privacy_box)
        self.store_transcript_check.setChecked(self._cfg.privacy.store_transcripts)
        pv.addWidget(self.store_transcript_check)

        self.log_transcript_check = QCheckBox("把语音内容写入日志（默认关闭，排错时才建议开启）", privacy_box)
        self.log_transcript_check.setChecked(self._cfg.privacy.log_transcripts)
        pv.addWidget(self.log_transcript_check)

        self.show_transcript_check = QCheckBox("在主界面显示最近识别到的文本（仅内存显示，不落盘）", privacy_box)
        self.show_transcript_check.setChecked(self._cfg.privacy.show_transcript_in_ui)
        pv.addWidget(self.show_transcript_check)

        self.save_raw_check = QCheckBox("保存原始麦克风录音到磁盘（默认关闭）", privacy_box)
        self.save_raw_check.setChecked(self._cfg.mic.save_raw_audio)
        pv.addWidget(self.save_raw_check)
        layout.addWidget(privacy_box)

        log_box = QGroupBox("日志与诊断", page)
        lf = QFormLayout(log_box)
        self.log_level_combo = QComboBox(log_box)
        self.log_level_combo.addItems(["DEBUG", "INFO", "WARNING", "ERROR"])
        self.log_level_combo.setCurrentText(self._cfg.logging.level)
        lf.addRow("日志级别", self.log_level_combo)

        self.log_path_label = QLabel(str(log_file_path(default_log_dir())), log_box)
        self.log_path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lf.addRow("日志文件", self.log_path_label)

        open_button = QPushButton("打开日志目录", log_box)
        open_button.clicked.connect(self._open_log_dir)
        lf.addRow("", open_button)

        note = QLabel("日志中的密码 / API Key 会被自动脱敏为 ***REDACTED***。", log_box)
        note.setWordWrap(True)
        lf.addRow(note)
        layout.addWidget(log_box)
        layout.addStretch(1)
        return page

    def _open_log_dir(self) -> None:
        target = default_log_dir()
        target.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(target))  # type: ignore[attr-defined]
        except Exception:  # pragma: no cover
            subprocess.Popen(["explorer", str(target)])

    # ------------------------------------------------------------------ #
    # 保存
    # ------------------------------------------------------------------ #
    def _on_accept(self) -> None:
        existing = [item for item in self._tracks if Path(str(item.get("path", ""))).exists()]
        if self._tracks and not existing:
            answer = QMessageBox.question(
                self,
                "曲目文件缺失",
                "列表里所有曲目的文件都不存在了，仍要保存吗？\n（触发时会报错，可稍后重新添加）",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        edited = self.password_edit.text()
        if edited and self._secrets is not None and getattr(self._secrets, "available", False):
            if self.save_password_check.isChecked():
                self._new_password_set = edited != self._password_value
                self._password_value = edited
            else:
                self._new_password_set = False
                self._password_value = edited
        self.accept()

    def result_config(self) -> AppConfig:
        cfg = self._cfg
        cfg.audio.playlist = [dict(track) for track in self._tracks]
        cfg.audio.current_index = (
            min(max(0, self._current_index), len(self._tracks) - 1) if self._tracks else 0
        )
        cfg.audio.volume = round(self.volume_spin.value() / 100.0, 4)
        cfg.audio.output_device = str(self.device_combo.currentData() or "")
        cfg.audio.play_from_start = self.from_start_check.isChecked()
        cfg.audio.allow_repeat = self.loop_check.isChecked()

        cfg.obs.host = self.host_edit.text().strip() or "127.0.0.1"
        cfg.obs.port = int(self.port_spin.value())
        cfg.obs.control_enabled = bool(self.output_mode_combo.currentData())
        cfg.obs.game_source = self.game_source_combo.currentText().strip()
        cfg.obs.bgm_source = self.bgm_source_combo.currentText().strip()
        cfg.obs.mic_source = self.mic_source_combo.currentText().strip()
        cfg.obs.auto_reconnect = self.autoreconnect_check.isChecked()
        cfg.obs.reconnect_interval_s = float(self.reconnect_spin.value())
        cfg.obs.control_strategy = str(self.strategy_combo.currentData() or "mute")
        cfg.obs.duck_volume = round(self.duck_spin.value() / 100.0, 4)
        cfg.obs.restore_strategy = str(self.restore_combo.currentData() or "restore_original")

        cfg.privacy.cloud_consent = self.consent_check.isChecked()
        cfg.privacy.store_transcripts = self.store_transcript_check.isChecked()
        cfg.privacy.log_transcripts = self.log_transcript_check.isChecked()
        cfg.privacy.show_transcript_in_ui = self.show_transcript_check.isChecked()
        cfg.mic.save_raw_audio = self.save_raw_check.isChecked()
        cfg.logging.level = self.log_level_combo.currentText()

        # 检测页
        cfg.mic.device = str(self.mic_device_combo.currentData() or "")
        cfg.mic.sample_rate = int(self.mic_samplerate_combo.currentData() or 16000)
        cfg.mic.vad_sensitivity = round(float(self.vad_sensitivity_spin.value()), 3)
        cfg.stt.enabled = self.stt_enabled_check.isChecked()
        cfg.stt.backend = "faster_whisper" if cfg.stt.enabled else "none"
        cfg.stt.model_size = str(self.stt_model_combo.currentData() or "small")
        cfg.stt.compute_type = str(self.stt_compute_combo.currentData() or "int8")
        cfg.stt.language = self.stt_language_edit.text().strip() or "zh"
        cfg.stt.model_dir = self.stt_model_dir_edit.text().strip()

        cfg.keywords.enabled = self.keywords_enabled_check.isChecked()
        cfg.keywords.positives = _parse_phrases(self.keywords_positive_edit.toPlainText())
        cfg.keywords.exclusions = _parse_phrases(self.keywords_exclusion_edit.toPlainText())
        cfg.keywords.min_hits = int(self.keywords_min_hits_spin.value())
        cfg.rules.enabled = self.rules_enabled_check.isChecked()

        raw_weights = {
            "ai": self.weight_ai_spin.value() / 100.0,
            "keyword": self.weight_keyword_spin.value() / 100.0,
            "rule": self.weight_rule_spin.value() / 100.0,
        }
        total = sum(raw_weights.values())
        if total <= 0:
            raw_weights = {"ai": 0.55, "keyword": 0.3, "rule": 0.15}
            total = 1.0
        cfg.trigger.weights = {key: round(value / total, 4) for key, value in raw_weights.items()}
        cfg.trigger.min_voice_seconds = float(self.min_voice_spin.value())
        cfg.trigger.dedupe_window_seconds = float(self.dedupe_spin.value())

        # AI 层
        cfg.ai.enabled = self.ai_enabled_check.isChecked()
        cfg.ai.backend = str(self.ai_backend_combo.currentData() or "none")
        if cfg.ai.enabled and cfg.ai.backend == "none":
            cfg.ai.enabled = False
        cfg.ai.base_url = self.ai_base_url_edit.text().strip()
        cfg.ai.model = self.ai_model_edit.text().strip()
        cfg.ai.timeout_seconds = float(self.ai_timeout_spin.value())
        cfg.ai.max_context = int(self.ai_context_spin.value())

        # 快捷键
        cfg.hotkeys.enabled = self.hotkeys_enabled_check.isChecked()
        cfg.hotkeys.force_trigger = self.hotkey_force_edit.keySequence().toString() or "F8"
        cfg.hotkeys.stop_music = self.hotkey_stop_edit.keySequence().toString() or "F9"
        cfg.hotkeys.toggle_auto = self.hotkey_auto_edit.keySequence().toString() or "F10"
        cfg.hotkeys.global_enabled = self.hotkeys_global_check.isChecked()
        return cfg

    # -- AI Key 的对外接口（MainWindow 用它决定要不要落盘） ------------------ #
    def ai_key_value(self) -> str:
        return self.ai_key_edit.text().strip()

    def ai_key_changed(self) -> bool:
        return self.ai_key_value() != self._saved_ai_key

    def should_save_ai_key(self) -> bool:
        return self.save_ai_key_check.isChecked()

    def password_value(self) -> str:
        return self._password_value

    def password_changed(self) -> bool:
        return self._new_password_set

    def should_save_password(self) -> bool:
        return self.save_password_check.isChecked()

"""配置的加载、类型/范围校验、原子保存与版本迁移。

设计要点：
- 代码内的 dataclass 默认值就是最终兜底，模板文件缺失也能启动。
- 任何非法字段都会被纠正为安全值，并把原因记录到 ``issues``，UI 可展示。
- 配置文件损坏（JSON 语法错误 / 顶层不是对象 / 版本来自未来）时先备份再抛
  :class:`ConfigCorruptError`，由 UI 决定是否恢复默认，绝不让程序直接崩溃。
- 写入使用临时文件 + ``os.replace``，避免掉电写出半个文件。
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

CURRENT_CONFIG_VERSION = 3

VALID_MODES = ("serious", "chaotic")
VALID_STRATEGIES = ("mute", "duck")
VALID_RESTORE_STRATEGIES = ("restore_original", "force_unmute")
VALID_BUSY_POLICIES = ("ignore", "queue")
VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
VALID_AI_BACKENDS = ("none", "openai_compatible", "local_llm")
VALID_STT_BACKENDS = ("none", "faster_whisper")

# 内置示例词库：模板文件缺失时也能工作。仅作示例，用户可在设置里增删。
DEFAULT_POSITIVE_KEYWORDS: list[dict[str, Any]] = [
    {"phrase": "别管我", "weight": 1.0, "aliases": ["别救我", "不要救我", "别理我", "不用管我", "别管我了"]},
    {"phrase": "你们快走", "weight": 1.0, "aliases": ["你们走吧", "你们先走", "快跑吧"]},
    {"phrase": "快走", "weight": 0.5, "aliases": ["快跑", "赶紧走"]},
    {"phrase": "我来拖住", "weight": 1.0, "aliases": ["我拖住", "我拦住他们", "我来顶住"]},
    {"phrase": "拖住他们", "weight": 0.8, "aliases": ["拖住他", "拦住他们"]},
    {"phrase": "我尽力了", "weight": 1.0, "aliases": ["已经尽力了", "我尽力啦", "我尽力了呀"]},
    {"phrase": "替我报仇", "weight": 1.0, "aliases": ["帮我报仇", "给我报仇", "替我报仇啊"]},
    {"phrase": "我撑不住了", "weight": 0.9, "aliases": ["我顶不住了", "撑不住了", "我不行了"]},
    {"phrase": "至少我做到了", "weight": 0.8, "aliases": ["至少我尽力了", "至少做到了"]},
    {"phrase": "照顾好我妈", "weight": 1.0, "aliases": ["照顾好我爸妈", "照顾好我妈咪"]},
    {"phrase": "不要管我", "weight": 1.0, "aliases": ["不用管我", "不要管我了"]},
    {"phrase": "这次真的要走了", "weight": 0.7, "aliases": ["真的要走了", "这次真走了"]},
]

DEFAULT_EXCLUSIONS: list[dict[str, Any]] = [
    {"phrase": "别管我吃什么", "weight": 1.0},
    {"phrase": "别管我睡觉", "weight": 1.0},
    {"phrase": "我尽力了但是", "weight": 0.5},
    {"phrase": "开玩笑", "weight": 1.0, "aliases": ["开个玩笑", "逗你玩", "说着玩"]},
    {"phrase": "哈哈", "weight": 0.6, "aliases": ["哈哈哈", "hhhh"]},
    {"phrase": "笑死", "weight": 0.6, "aliases": ["笑死我了", "笑死我"]},
    {"phrase": "队友呢", "weight": 0.4, "aliases": ["队友在哪", "队友去哪了"]},
]

# 老配置升级用：phrase -> 常见近义说法。只用于「用户词条没有 aliases 时补全」，
# 不会覆盖用户自己写的近义词。
DEFAULT_ALIASES: dict[str, list[str]] = {
    item["phrase"]: list(item.get("aliases", []))
    for item in (*DEFAULT_POSITIVE_KEYWORDS, *DEFAULT_EXCLUSIONS)
    if item.get("aliases")
}


class ConfigError(Exception):
    """配置相关错误的基类。"""


class ConfigCorruptError(ConfigError):
    """配置文件内容不可用（已备份原文件）。"""

    def __init__(self, message: str, backup_path: Path | None = None) -> None:
        super().__init__(message)
        self.backup_path = backup_path


# --------------------------------------------------------------------------- #
# 校验辅助
# --------------------------------------------------------------------------- #
def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "是", "开")
    return default


def _as_int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        out = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, out))


def _as_float(value: Any, default: float, lo: float, hi: float) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if out != out:  # NaN
        return default
    return max(lo, min(hi, out))


def _as_choice(value: Any, default: str, allowed: tuple[str, ...]) -> str:
    if isinstance(value, str) and value.strip().lower() in allowed:
        return value.strip().lower()
    return default


def _as_str(value: Any, default: str = "") -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return default
    return str(value)


def _as_alias_list(value: Any) -> list[str]:
    """近义说法：支持列表，或用 , ， 、 ; 分隔的字符串。"""
    if isinstance(value, str):
        parts = re.split(r"[,，、;；]| / ", value)
    elif isinstance(value, (list, tuple)):
        parts = [str(item) for item in value]
    else:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        text = part.strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _as_phrase_list(value: Any, default: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """关键词列表：过滤空词组、去重、权重收敛到 [0, 2]，支持近义说法 aliases。"""
    if not isinstance(value, list):
        return [{**item} for item in default]
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in value:
        if isinstance(item, str):
            phrase, weight, aliases = item, 1.0, []
        elif isinstance(item, dict):
            phrase = _as_str(item.get("phrase", "")).strip()
            weight = _as_float(item.get("weight", 1.0), 1.0, 0.0, 2.0)
            aliases = _as_alias_list(item.get("aliases"))
        else:
            continue
        if not phrase or phrase in seen:
            continue
        seen.add(phrase)
        entry: dict[str, Any] = {"phrase": phrase, "weight": weight}
        clean_aliases = [alias for alias in aliases if alias != phrase]
        if clean_aliases:
            entry["aliases"] = clean_aliases
        out.append(entry)
    return out


def _as_track_list(value: Any) -> list[dict[str, Any]]:
    """曲目列表：过滤空路径、去重、补全标题。"""
    if not isinstance(value, list):
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in value:
        if isinstance(item, str):
            path, title = item.strip(), ""
        elif isinstance(item, dict):
            path = _as_str(item.get("path", "")).strip()
            title = _as_str(item.get("title", "")).strip()
        else:
            continue
        if not path or path in seen:
            continue
        seen.add(path)
        out.append({"path": path, "title": title or Path(path).stem})
    return out


def _normalized_weights(value: Any, default: dict[str, float]) -> dict[str, float]:
    """权重必须非负并归一化，避免「未归一化数值直接相加」。"""
    base = dict(default)
    if isinstance(value, dict):
        for key in base:
            if key in value:
                base[key] = _as_float(value[key], base[key], 0.0, 10.0)
    total = sum(base.values())
    if total <= 0:
        base = dict(default)
        total = sum(base.values()) or 1.0
    return {key: round(val / total, 6) for key, val in base.items()}


# --------------------------------------------------------------------------- #
# 配置段
# --------------------------------------------------------------------------- #
@dataclass
class AudioConfig:
    """BGM 播放列表 + 播放参数。

    从 0.1 的单曲 ``bgm_path`` 演变而来：旧配置会在 ``from_dict`` 里自动迁移成
    playlist 的第一条，``bgm_path`` 属性保留为「当前曲目路径」的只读视图，
    这样事件管理器/设置页不需要知道列表的存在。
    """

    playlist: list[dict[str, Any]] = field(default_factory=list)
    current_index: int = 0
    volume: float = 0.8
    output_device: str = ""
    play_from_start: bool = True
    allow_repeat: bool = False
    discovered: bool = False
    """是否已经做过「自动收进本地音乐」的一次性发现（用户清空列表后不再自动填充）。"""

    # -- 曲目视图 ----------------------------------------------------------- #
    @property
    def current_track(self) -> dict[str, Any] | None:
        if not self.playlist:
            return None
        index = min(max(0, self.current_index), len(self.playlist) - 1)
        return self.playlist[index]

    @property
    def bgm_path(self) -> str:
        track = self.current_track
        return str(track.get("path", "")) if track else ""

    @property
    def bgm_title(self) -> str:
        track = self.current_track
        if not track:
            return ""
        return str(track.get("title") or Path(str(track.get("path", ""))).stem)

    def set_current(self, index: int) -> None:
        if not self.playlist:
            self.current_index = 0
            return
        self.current_index = min(max(0, int(index)), len(self.playlist) - 1)

    def add_track(self, path: str, title: str = "") -> int:
        """追加曲目并返回其索引；已存在同名路径时不重复添加。"""
        clean = str(path).strip()
        if not clean:
            return max(0, len(self.playlist) - 1)
        for index, track in enumerate(self.playlist):
            if str(track.get("path", "")) == clean:
                return index
        self.playlist.append(
            {"path": clean, "title": (title or Path(clean).stem).strip()}
        )
        return len(self.playlist) - 1

    def remove_track(self, index: int) -> None:
        if not self.playlist:
            return
        index = min(max(0, int(index)), len(self.playlist) - 1)
        self.playlist.pop(index)
        self.set_current(min(self.current_index, len(self.playlist) - 1) if self.playlist else 0)

    def move_track(self, index: int, delta: int) -> int:
        if not self.playlist:
            return 0
        source = min(max(0, int(index)), len(self.playlist) - 1)
        target = min(max(0, source + int(delta)), len(self.playlist) - 1)
        if source != target:
            track = self.playlist.pop(source)
            self.playlist.insert(target, track)
            if self.current_index == source:
                self.current_index = target
            elif source < self.current_index <= target:
                self.current_index -= 1
            elif target <= self.current_index < source:
                self.current_index += 1
        return target

    @classmethod
    def from_dict(cls, raw: Any, issues: list[str]) -> "AudioConfig":
        raw = raw if isinstance(raw, dict) else {}
        default = cls()
        playlist = _as_track_list(raw.get("playlist"))
        if not playlist:
            legacy_path = _as_str(raw.get("bgm_path", ""), "").strip()
            if legacy_path:
                playlist = [{"path": legacy_path, "title": Path(legacy_path).stem}]
                issues.append("检测到旧版单曲配置，已迁移为播放列表的第一首")
        index = _as_int(raw.get("current_index", 0), 0, 0, max(0, len(playlist) - 1))
        vol = _as_float(raw.get("volume", default.volume), default.volume, 0.0, 1.0)
        return cls(
            playlist=playlist,
            current_index=index,
            volume=vol,
            output_device=_as_str(raw.get("output_device", ""), "").strip(),
            play_from_start=_as_bool(raw.get("play_from_start", True), True),
            allow_repeat=_as_bool(raw.get("allow_repeat", False), False),
            discovered=_as_bool(raw.get("discovered", False), False),
        )


@dataclass
class OBSConfig:
    host: str = "127.0.0.1"
    port: int = 4455
    game_source: str = "桌面音频"
    bgm_source: str = ""
    mic_source: str = ""
    auto_reconnect: bool = True
    reconnect_interval_s: float = 5.0
    control_strategy: str = "mute"
    duck_volume: float = 0.15
    restore_strategy: str = "restore_original"
    control_enabled: bool = True
    """False = 纯本地模式：完全不碰 OBS，只做「听到台词就放歌」。"""

    @classmethod
    def from_dict(cls, raw: Any, issues: list[str]) -> "OBSConfig":
        raw = raw if isinstance(raw, dict) else {}
        default = cls()
        host = _as_str(raw.get("host", default.host), default.host).strip() or default.host
        strategy = _as_choice(raw.get("control_strategy"), default.control_strategy, VALID_STRATEGIES)
        if strategy != default.control_strategy:
            issues.append(f"control_strategy 非默认值：{strategy}")
        return cls(
            host=host,
            port=_as_int(raw.get("port", default.port), default.port, 1, 65535),
            game_source=_as_str(raw.get("game_source", default.game_source), "").strip(),
            bgm_source=_as_str(raw.get("bgm_source", ""), "").strip(),
            mic_source=_as_str(raw.get("mic_source", ""), "").strip(),
            auto_reconnect=_as_bool(raw.get("auto_reconnect", True), True),
            reconnect_interval_s=_as_float(
                raw.get("reconnect_interval_s", 5.0), 5.0, 0.5, 120.0
            ),
            control_strategy=strategy,
            duck_volume=_as_float(raw.get("duck_volume", 0.15), 0.15, 0.0, 1.0),
            restore_strategy=_as_choice(
                raw.get("restore_strategy"), default.restore_strategy, VALID_RESTORE_STRATEGIES
            ),
            control_enabled=_as_bool(raw.get("control_enabled", True), True),
        )


@dataclass
class ModeConfig:
    current: str = "serious"
    auto_trigger_enabled: bool = True
    global_mute: bool = False

    @classmethod
    def from_dict(cls, raw: Any, issues: list[str]) -> "ModeConfig":
        raw = raw if isinstance(raw, dict) else {}
        default = cls()
        mode = _as_choice(raw.get("current"), default.current, VALID_MODES)
        if mode == "chaotic":
            issues.append("当前为抽象整活模式：普通语句也可能触发")
        return cls(
            current=mode,
            auto_trigger_enabled=_as_bool(raw.get("auto_trigger_enabled", True), True),
            global_mute=_as_bool(raw.get("global_mute", False), False),
        )


@dataclass
class TriggerConfig:
    cooldown_seconds: float = 30.0
    trigger_threshold: float = 0.6
    low_confidence_threshold: float = 0.35
    min_voice_seconds: float = 0.8
    dedupe_window_seconds: float = 8.0
    ai_timeout_seconds: float = 4.0
    max_event_seconds: float = 300.0
    busy_policy: str = "ignore"
    weights: dict[str, float] = field(
        default_factory=lambda: {"ai": 0.55, "keyword": 0.3, "rule": 0.15}
    )
    probabilities: dict[str, float] = field(
        default_factory=lambda: {
            "serious": 1.0,
            "low_confidence": 0.35,
            "chaotic": 0.5,
            "chaotic_low_score": 0.2,
            "ai_unavailable": 0.6,
        }
    )

    @classmethod
    def from_dict(cls, raw: Any, issues: list[str]) -> "TriggerConfig":
        raw = raw if isinstance(raw, dict) else {}
        default = cls()
        trigger_threshold = _as_float(
            raw.get("trigger_threshold", default.trigger_threshold), default.trigger_threshold, 0.0, 1.0
        )
        low = _as_float(
            raw.get("low_confidence_threshold", default.low_confidence_threshold),
            default.low_confidence_threshold,
            0.0,
            1.0,
        )
        if low > trigger_threshold:
            issues.append("low_confidence_threshold 高于 trigger_threshold，已调整")
            low = trigger_threshold
        raw_probs = raw.get("probabilities", {}) if isinstance(raw.get("probabilities"), dict) else {}
        probs = {
            key: _as_float(raw_probs.get(key, val), val, 0.0, 1.0)
            for key, val in default.probabilities.items()
        }
        return cls(
            cooldown_seconds=_as_float(
                raw.get("cooldown_seconds", default.cooldown_seconds), default.cooldown_seconds, 0.0, 3600.0
            ),
            trigger_threshold=trigger_threshold,
            low_confidence_threshold=low,
            min_voice_seconds=_as_float(
                raw.get("min_voice_seconds", default.min_voice_seconds), default.min_voice_seconds, 0.0, 30.0
            ),
            dedupe_window_seconds=_as_float(
                raw.get("dedupe_window_seconds", default.dedupe_window_seconds),
                default.dedupe_window_seconds,
                0.0,
                120.0,
            ),
            ai_timeout_seconds=_as_float(
                raw.get("ai_timeout_seconds", default.ai_timeout_seconds), default.ai_timeout_seconds, 0.2, 60.0
            ),
            max_event_seconds=_as_float(
                raw.get("max_event_seconds", default.max_event_seconds), default.max_event_seconds, 5.0, 3600.0
            ),
            busy_policy=_as_choice(raw.get("busy_policy"), default.busy_policy, VALID_BUSY_POLICIES),
            weights=_normalized_weights(raw.get("weights"), default.weights),
            probabilities=probs,
        )


@dataclass
class KeywordsConfig:
    enabled: bool = True
    min_hits: int = 1
    case_sensitive: bool = False
    positives: list[dict[str, Any]] = field(
        default_factory=lambda: [dict(item) for item in DEFAULT_POSITIVE_KEYWORDS]
    )
    exclusions: list[dict[str, Any]] = field(
        default_factory=lambda: [dict(item) for item in DEFAULT_EXCLUSIONS]
    )

    @classmethod
    def from_dict(cls, raw: Any, issues: list[str]) -> "KeywordsConfig":
        raw = raw if isinstance(raw, dict) else {}
        default = cls()
        positives = _as_phrase_list(raw.get("positives"), default.positives)
        exclusions = _as_phrase_list(raw.get("exclusions"), default.exclusions)
        if not positives:
            issues.append("关键词库为空，关键词层不会产生任何分数")
        return cls(
            enabled=_as_bool(raw.get("enabled", True), True),
            min_hits=_as_int(raw.get("min_hits", 1), 1, 1, 20),
            case_sensitive=_as_bool(raw.get("case_sensitive", False), False),
            positives=positives,
            exclusions=exclusions,
        )


@dataclass
class RulesConfig:
    enabled: bool = True
    weights: dict[str, float] = field(
        default_factory=lambda: {
            "speech_duration": 0.25,
            "pause_after": 0.25,
            "volume_drop": 0.2,
            "volume_spike": 0.1,
            "silence_after": 0.2,
        }
    )
    speech_duration_full_score_s: float = 4.0
    long_pause_s: float = 1.2
    volume_drop_ratio: float = 0.5

    @classmethod
    def from_dict(cls, raw: Any, issues: list[str]) -> "RulesConfig":
        raw = raw if isinstance(raw, dict) else {}
        default = cls()
        return cls(
            enabled=_as_bool(raw.get("enabled", True), True),
            weights=_normalized_weights(raw.get("weights"), default.weights),
            speech_duration_full_score_s=_as_float(
                raw.get("speech_duration_full_score_s", 4.0), 4.0, 0.5, 60.0
            ),
            long_pause_s=_as_float(raw.get("long_pause_s", 1.2), 1.2, 0.1, 10.0),
            volume_drop_ratio=_as_float(raw.get("volume_drop_ratio", 0.5), 0.5, 0.0, 1.0),
        )


@dataclass
class AIConfig:
    enabled: bool = False
    backend: str = "none"
    model: str = ""
    base_url: str = ""
    timeout_seconds: float = 4.0
    max_context: int = 3
    system_prompt: str = ""

    @classmethod
    def from_dict(cls, raw: Any, issues: list[str]) -> "AIConfig":
        raw = raw if isinstance(raw, dict) else {}
        default = cls()
        backend = _as_choice(raw.get("backend"), default.backend, VALID_AI_BACKENDS)
        enabled = _as_bool(raw.get("enabled", False), False)
        if enabled and backend == "none":
            issues.append("AI 分析已启用但未选择后端，已退回规则模式")
            enabled = False
        if enabled and backend == "openai_compatible":
            issues.append("已选择云端 AI：语音文本会发送到第三方服务，请确认隐私授权")
        model = _as_str(raw.get("model", ""), "").strip()
        if enabled and not model:
            issues.append("AI 已启用但未填写模型名称，将无法分析")
        return cls(
            enabled=enabled,
            backend=backend,
            model=model,
            base_url=_as_str(raw.get("base_url", ""), "").strip(),
            timeout_seconds=_as_float(raw.get("timeout_seconds", 4.0), 4.0, 0.5, 60.0),
            max_context=_as_int(raw.get("max_context", 3), 3, 0, 10),
            system_prompt=_as_str(raw.get("system_prompt", ""), ""),
        )


@dataclass
class MicConfig:
    enabled: bool = False
    device: str = ""
    sample_rate: int = 16000
    save_raw_audio: bool = False
    vad_sensitivity: float = 0.5

    @classmethod
    def from_dict(cls, raw: Any, issues: list[str]) -> "MicConfig":
        raw = raw if isinstance(raw, dict) else {}
        default = cls()
        save = _as_bool(raw.get("save_raw_audio", False), False)
        if save:
            issues.append("已启用原始录音保存：这将把麦克风音频写入磁盘")
        return cls(
            enabled=_as_bool(raw.get("enabled", False), False),
            device=_as_str(raw.get("device", ""), "").strip(),
            sample_rate=_as_int(raw.get("sample_rate", default.sample_rate), default.sample_rate, 8000, 48000),
            save_raw_audio=save,
            vad_sensitivity=_as_float(
                raw.get("vad_sensitivity", default.vad_sensitivity), default.vad_sensitivity, 0.0, 1.0
            ),
        )


@dataclass
class STTConfig:
    enabled: bool = False
    backend: str = "none"
    model_size: str = "small"
    language: str = "zh"
    model_dir: str = ""
    compute_type: str = "int8"
    initial_prompt: str = "以下是普通话的句子。"

    @classmethod
    def from_dict(cls, raw: Any, issues: list[str]) -> "STTConfig":
        raw = raw if isinstance(raw, dict) else {}
        default = cls()
        backend = _as_choice(raw.get("backend"), default.backend, VALID_STT_BACKENDS)
        enabled = _as_bool(raw.get("enabled", False), False)
        if enabled and backend == "none":
            issues.append("语音识别已启用但未选择后端，已关闭")
            enabled = False
        compute = _as_choice(
            raw.get("compute_type"), "int8", ("int8", "int8_float16", "float16", "float32")
        )
        return cls(
            enabled=enabled,
            backend=backend,
            model_size=_as_str(raw.get("model_size", "small"), "small").strip() or "small",
            language=_as_str(raw.get("language", "zh"), "zh").strip() or "zh",
            model_dir=_as_str(raw.get("model_dir", ""), "").strip(),
            compute_type=compute,
            initial_prompt=_as_str(raw.get("initial_prompt", default.initial_prompt), default.initial_prompt),
        )


@dataclass
class PrivacyConfig:
    cloud_consent: bool = False
    store_transcripts: bool = False
    log_transcripts: bool = False
    show_transcript_in_ui: bool = True

    @classmethod
    def from_dict(cls, raw: Any, issues: list[str]) -> "PrivacyConfig":
        raw = raw if isinstance(raw, dict) else {}
        return cls(
            cloud_consent=_as_bool(raw.get("cloud_consent", False), False),
            store_transcripts=_as_bool(raw.get("store_transcripts", False), False),
            log_transcripts=_as_bool(raw.get("log_transcripts", False), False),
            show_transcript_in_ui=_as_bool(raw.get("show_transcript_in_ui", True), True),
        )


DEFAULT_HOTKEYS = {"force_trigger": "F8", "stop_music": "F9", "toggle_auto": "F10"}


@dataclass
class HotkeyConfig:
    enabled: bool = True
    force_trigger: str = "F8"
    stop_music: str = "F9"
    toggle_auto: str = "F10"
    global_enabled: bool = False
    """全局热键默认关闭：RegisterHotKey 会独占按键，可能干扰游戏操作。"""

    @classmethod
    def from_dict(cls, raw: Any, issues: list[str]) -> "HotkeyConfig":
        raw = raw if isinstance(raw, dict) else {}
        values = {
            key: _as_str(raw.get(key, default), default).strip() or default
            for key, default in DEFAULT_HOTKEYS.items()
        }
        seen: dict[str, str] = {}
        for key, value in values.items():
            norm = value.upper()
            if norm in seen:
                issues.append(f"快捷键冲突：{key} 与 {seen[norm]} 都是 {value}，已禁用 {key}")
                values[key] = ""
            else:
                seen[norm] = key
        return cls(
            enabled=_as_bool(raw.get("enabled", True), True),
            global_enabled=_as_bool(raw.get("global_enabled", False), False),
            **values,
        )


@dataclass
class LoggingConfig:
    level: str = "INFO"
    dir: str = ""
    max_entries: int = 200

    @classmethod
    def from_dict(cls, raw: Any, issues: list[str]) -> "LoggingConfig":
        raw = raw if isinstance(raw, dict) else {}
        level = _as_str(raw.get("level", "INFO"), "INFO").strip().upper()
        if level not in VALID_LOG_LEVELS:
            issues.append(f"日志级别非法：{level}，已改为 INFO")
            level = "INFO"
        return cls(
            level=level,
            dir=_as_str(raw.get("dir", ""), "").strip(),
            max_entries=_as_int(raw.get("max_entries", 200), 200, 10, 10000),
        )


@dataclass
class AppConfig:
    version: int = CURRENT_CONFIG_VERSION
    audio: AudioConfig = field(default_factory=AudioConfig)
    obs: OBSConfig = field(default_factory=OBSConfig)
    mode: ModeConfig = field(default_factory=ModeConfig)
    trigger: TriggerConfig = field(default_factory=TriggerConfig)
    keywords: KeywordsConfig = field(default_factory=KeywordsConfig)
    rules: RulesConfig = field(default_factory=RulesConfig)
    ai: AIConfig = field(default_factory=AIConfig)
    mic: MicConfig = field(default_factory=MicConfig)
    stt: STTConfig = field(default_factory=STTConfig)
    privacy: PrivacyConfig = field(default_factory=PrivacyConfig)
    hotkeys: HotkeyConfig = field(default_factory=HotkeyConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["version"] = CURRENT_CONFIG_VERSION
        return data

    @classmethod
    def from_dict(cls, raw: Any) -> tuple["AppConfig", list[str]]:
        issues: list[str] = []
        if not isinstance(raw, dict):
            raise ConfigCorruptError("配置顶层不是 JSON 对象")
        version = _as_int(raw.get("version", 1), 1, 0, 10_000)
        if version > CURRENT_CONFIG_VERSION:
            raise ConfigCorruptError(
                f"配置版本 {version} 高于本程序支持的版本 {CURRENT_CONFIG_VERSION}"
            )
        source, migration_issues = _migrate(raw, version)
        issues.extend(migration_issues)
        cfg = cls(
            version=CURRENT_CONFIG_VERSION,
            audio=AudioConfig.from_dict(source.get("audio"), issues),
            obs=OBSConfig.from_dict(source.get("obs"), issues),
            mode=ModeConfig.from_dict(source.get("mode"), issues),
            trigger=TriggerConfig.from_dict(source.get("trigger"), issues),
            keywords=KeywordsConfig.from_dict(source.get("keywords"), issues),
            rules=RulesConfig.from_dict(source.get("rules"), issues),
            ai=AIConfig.from_dict(source.get("ai"), issues),
            mic=MicConfig.from_dict(source.get("mic"), issues),
            stt=STTConfig.from_dict(source.get("stt"), issues),
            privacy=PrivacyConfig.from_dict(source.get("privacy"), issues),
            hotkeys=HotkeyConfig.from_dict(source.get("hotkeys"), issues),
            logging=LoggingConfig.from_dict(source.get("logging"), issues),
        )
        return cfg, issues


def _migrate(raw: dict[str, Any], version: int) -> tuple[dict[str, Any], list[str]]:
    """按版本逐级迁移配置。

    v1 → v2：把单曲 ``audio.bgm_path`` 挪进 ``audio.playlist``（曲目列表）。
    v2 → v3：给内置关键词补上常见近义说法（用户自己写过的 aliases 不覆盖）。
    """
    data = dict(raw)
    issues: list[str] = []
    if version < 2:
        audio = data.get("audio")
        if isinstance(audio, dict):
            legacy = _as_str(audio.get("bgm_path", ""), "").strip()
            existing = audio.get("playlist")
            if legacy and not (isinstance(existing, list) and existing):
                migrated = dict(audio)
                migrated["playlist"] = [{"path": legacy, "title": Path(legacy).stem}]
                migrated.setdefault("current_index", 0)
                migrated.pop("bgm_path", None)
                data["audio"] = migrated
                issues.append("配置已从 v1 迁移到 v2：单曲路径转成播放列表的第一首")
    if version < 3:
        keywords = data.get("keywords")
        if isinstance(keywords, dict):
            patched_count = 0
            for key in ("positives", "exclusions"):
                items = keywords.get(key)
                if not isinstance(items, list):
                    continue
                patched: list[Any] = []
                for item in items:
                    if isinstance(item, dict):
                        phrase = _as_str(item.get("phrase", ""), "").strip()
                        has_alias = bool(_as_alias_list(item.get("aliases")))
                        default_aliases = DEFAULT_ALIASES.get(phrase)
                        if phrase and not has_alias and default_aliases:
                            item = {**item, "aliases": list(default_aliases)}
                            patched_count += 1
                    patched.append(item)
                keywords[key] = patched
            if patched_count:
                issues.append(
                    f"配置已从 v2 迁移到 v3：为 {patched_count} 个关键词补充了常见近义说法"
                )
    data["version"] = CURRENT_CONFIG_VERSION
    return data, issues


# --------------------------------------------------------------------------- #
# 管理器
# --------------------------------------------------------------------------- #
def default_config_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "RustyMoment"


def default_template_path() -> Path:
    """默认配置模板路径：兼容源码运行与 PyInstaller 打包后的目录结构。"""
    if getattr(sys, "frozen", False):  # PyInstaller
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        return base / "config" / "default_config.json"
    return Path(__file__).resolve().parents[2] / "config" / "default_config.json"


class ConfigManager:
    """配置的读写入口。线程安全性：仅允许主线程/工作线程串行调用。"""

    def __init__(self, config_path: Path | None = None, template_path: Path | None = None) -> None:
        self.path = Path(config_path) if config_path else default_config_dir() / "config.json"
        self.template_path = Path(template_path) if template_path else default_template_path()
        self.issues: list[str] = []

    # -- 读 --------------------------------------------------------------- #
    def load(self) -> AppConfig:
        cfg, issues = self.load_with_issues()
        self.issues = issues
        for item in issues:
            log.warning("配置校验：%s", item)
        return cfg

    def load_with_issues(self) -> tuple[AppConfig, list[str]]:
        if not self.path.exists():
            cfg = self.default_config()
            self.save(cfg)
            return cfg, ["未找到配置文件，已生成默认配置"]
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
            backup = self._backup_corrupt()
            raise ConfigCorruptError(f"配置文件无法解析：{exc}", backup) from exc
        try:
            cfg, issues = AppConfig.from_dict(raw)
        except ConfigCorruptError as exc:
            backup = self._backup_corrupt()
            exc.backup_path = backup
            raise
        cfg, extra_issues = self._ensure_default_tracks(cfg)
        raw_version = _as_int(raw.get("version", 1), 1, 0, 10_000) if isinstance(raw, dict) else 1
        if raw_version < CURRENT_CONFIG_VERSION:
            # 迁移完成后写回磁盘，避免每次启动都重复迁移
            try:
                self.save(cfg)
            except OSError as exc:  # pragma: no cover
                log.warning("迁移后的配置保存失败：%s", exc)
        return cfg, issues + extra_issues

    def _ensure_default_tracks(self, cfg: AppConfig) -> tuple[AppConfig, list[str]]:
        """首次运行/升级时把本地音乐收进列表（只做一次，用户清空后不再打扰）。"""
        issues: list[str] = []
        if cfg.audio.discovered or cfg.audio.playlist:
            if not cfg.audio.discovered:
                cfg.audio.discovered = True
            return cfg, issues
        discovered = self._discover_default_tracks()
        cfg.audio.discovered = True
        if discovered:
            cfg.audio.playlist = discovered
            cfg.audio.current_index = 0
            issues.append(f"已自动收进 {len(discovered)} 首本地音乐作为默认曲目")
            try:
                self.save(cfg)
            except OSError as exc:  # pragma: no cover
                log.warning("保存默认曲目失败：%s", exc)
        return cfg, issues

    def default_config(self) -> AppConfig:
        """优先用模板文件，模板有问题时退回代码内默认值。

        若最终没有任何曲目，则扫描系统音乐目录，最多收三首作为默认曲目
        （用户要求「默认有三首曲子」）。
        """
        cfg: AppConfig | None = None
        if self.template_path.exists():
            try:
                raw = json.loads(self.template_path.read_text(encoding="utf-8"))
                parsed, issues = AppConfig.from_dict(raw)
                if issues:
                    log.info("模板配置校验提示：%s", issues)
                cfg = parsed
            except (json.JSONDecodeError, OSError, ConfigError) as exc:
                log.warning("默认配置模板不可用（%s），改用内置默认值", exc)
        if cfg is None:
            cfg = AppConfig()
        if not cfg.audio.playlist:
            cfg.audio.playlist = self._discover_default_tracks()
            cfg.audio.current_index = 0
        cfg.audio.discovered = True
        return cfg

    @staticmethod
    def _discover_default_tracks() -> list[dict[str, Any]]:
        try:
            from ..audio.music_library import discover_tracks

            return discover_tracks()
        except Exception as exc:  # pragma: no cover - 扫描失败不影响启动
            log.info("未能自动发现默认曲目：%s", exc)
            return []

    # -- 写 --------------------------------------------------------------- #
    def save(self, cfg: AppConfig) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2)
        fd, tmp_name = tempfile.mkstemp(dir=str(self.path.parent), prefix=".config-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, self.path)
        finally:
            if os.path.exists(tmp_name):
                try:
                    os.unlink(tmp_name)
                except OSError:  # pragma: no cover - 极端文件锁情况
                    pass

    def reset_to_default(self) -> AppConfig:
        cfg = self.default_config()
        self.save(cfg)
        self.issues = []
        return cfg

    def _backup_corrupt(self) -> Path | None:
        if not self.path.exists():
            return None
        backup = self.path.with_name(f"{self.path.stem}.corrupt-{int(time.time())}.json")
        try:
            self.path.replace(backup)
            log.error("配置文件损坏，已备份到 %s", backup)
            return backup
        except OSError:  # pragma: no cover
            return None

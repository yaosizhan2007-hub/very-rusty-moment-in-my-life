"""音频状态管理器：记录事件前的 OBS 音源状态，并在事件结束后安全恢复。

这是全项目最容易出事故的地方，规则写死在这里：

1. 记录原始状态（静音 + 音量），不用「无条件取消静音」这种粗暴恢复。
2. 事件期间由本模块管理临时状态；事件结束按快照恢复。
3. 如果用户在事件期间自己改了静音/音量，不覆盖用户的新设置，并在结果里报告。
4. OBS 断开时不继续发送指令，保留快照供重连后手动恢复。
5. 同一时刻只允许一个活动事件（重复触发由上层决定忽略或排队）。
6. 快照落盘一份：程序异常退出后，下次启动能告诉用户「上次可能还静音着」。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from ..obs.obs_client import OBSConnectionError, OBSRequestError, OBSTimeoutError
from ..obs.obs_controller import AudioSourceState, OBSSourceError

log = logging.getLogger(__name__)

VOLUME_TOLERANCE = 1e-3


class AudioStateError(Exception):
    """音频状态管理的通用错误。"""


class BusyStateError(AudioStateError):
    """已有活动事件，禁止再记录/覆盖状态。"""


class AudioControl(Protocol):
    """AudioStateManager 依赖的最小 OBS 控制接口（测试可注入 fake）。"""

    @property
    def connected(self) -> bool: ...

    def require_source(self, name: str) -> None: ...

    def get_state(self, name: str) -> AudioSourceState: ...

    def set_muted(self, name: str, muted: bool) -> None: ...

    def set_volume_mul(self, name: str, volume_mul: float) -> None: ...


@dataclass
class SourceSnapshot:
    source: str
    muted: bool
    volume_mul: float
    captured_at: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AppliedControl:
    source: str
    strategy: str
    muted: bool | None
    volume_mul: float | None


@dataclass
class RestoreOutcome:
    """恢复结果，供 UI 与事件日志显示。"""

    restored: list[str] = field(default_factory=list)
    skipped_user_modified: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    snapshot_kept: bool = False

    @property
    def ok(self) -> bool:
        return not self.failed

    def summary(self) -> str:
        parts: list[str] = []
        if self.restored:
            parts.append("已恢复：" + "、".join(self.restored))
        if self.skipped_user_modified:
            parts.append("用户已手动修改，跳过：" + "、".join(self.skipped_user_modified))
        if self.failed:
            parts.append("恢复失败：" + "、".join(self.failed))
        parts.extend(self.notes)
        return "；".join(parts) if parts else "无需恢复"


def default_state_path() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "RustyMoment" / "pending_audio_state.json"


class AudioStateManager:
    """单活动事件的音频状态管理器（线程安全）。"""

    def __init__(
        self,
        control: AudioControl,
        *,
        clock: Callable[[], float] = time.monotonic,
        persist_path: Path | None = None,
        restore_strategy: str = "restore_original",
    ) -> None:
        self._control = control
        self._clock = clock
        self._persist_path = Path(persist_path) if persist_path else default_state_path()
        self.restore_strategy = restore_strategy
        self._lock = threading.RLock()
        self._event_id: str | None = None
        self._snapshot: SourceSnapshot | None = None
        self._applied: AppliedControl | None = None

    # -- 查询 --------------------------------------------------------------- #
    @property
    def is_active(self) -> bool:
        return self._event_id is not None

    @property
    def active_event_id(self) -> str | None:
        return self._event_id

    @property
    def snapshot(self) -> SourceSnapshot | None:
        return self._snapshot

    @property
    def applied(self) -> AppliedControl | None:
        return self._applied

    # -- 记录 --------------------------------------------------------------- #
    def begin(self, event_id: str, source: str) -> SourceSnapshot:
        """记录事件前的原始状态。已有活动事件时抛 :class:`BusyStateError`。"""
        with self._lock:
            if self._event_id is not None:
                raise BusyStateError(
                    f"已有活动事件 {self._event_id}，拒绝覆盖音频快照"
                )
            if not self._control.connected:
                raise AudioStateError("OBS 未连接，无法记录音源状态")
            if not source:
                raise AudioStateError("未配置游戏音源")
            self._control.require_source(source)
            state = self._control.get_state(source)
            snapshot = SourceSnapshot(
                source=state.name,
                muted=state.muted,
                volume_mul=state.volume_mul,
                captured_at=self._clock(),
            )
            self._event_id = event_id
            self._snapshot = snapshot
            self._applied = None
            log.info(
                "已记录音源原始状态 %s(muted=%s, volume=%.3f)",
                snapshot.source,
                snapshot.muted,
                snapshot.volume_mul,
            )
            self._persist()
            return snapshot

    def apply_control(self, strategy: str, duck_volume: float) -> AppliedControl:
        """按策略切断游戏声音，并记住「我们写入了什么」用于识别用户手动改动。"""
        with self._lock:
            snapshot = self._snapshot
            if snapshot is None:
                raise AudioStateError("没有可控制的音频快照（事件尚未开始）")
            if not self._control.connected:
                raise AudioStateError("OBS 未连接，已停止发送音频控制指令")
            source = snapshot.source
            if strategy == "duck":
                target_volume = max(0.0, min(1.0, float(duck_volume)))
                self._control.set_volume_mul(source, target_volume)
                applied = AppliedControl(
                    source=source, strategy="duck", muted=None, volume_mul=target_volume
                )
            else:
                self._control.set_muted(source, True)
                applied = AppliedControl(source=source, strategy="mute", muted=True, volume_mul=None)
            self._applied = applied
            self._persist()
            log.info("已控制音源 %s（策略=%s）", source, strategy)
            return applied

    # -- 恢复 --------------------------------------------------------------- #
    def restore(self) -> RestoreOutcome:
        """恢复到事件前的状态。失败时保留快照，便于重连后重试。"""
        with self._lock:
            snapshot = self._snapshot
            applied = self._applied
            event_id = self._event_id
            if snapshot is None:
                return RestoreOutcome(notes=["没有待恢复的音频状态"])
            outcome = RestoreOutcome()
            source = snapshot.source

            if not self._control.connected:
                outcome.failed.append(source)
                outcome.notes.append("OBS 连接已断开，未能恢复；重连后请点击「重置音频状态」")
                outcome.snapshot_kept = True
                return outcome

            try:
                if self.restore_strategy == "force_unmute":
                    self._control.set_muted(source, False)
                    outcome.restored.append(f"{source}（强制取消静音）")
                    outcome.notes.append("已按配置强制取消静音，若原状态为静音请自行确认")
                else:
                    self._restore_snapshot(snapshot, applied, outcome)
            except (OBSRequestError, OBSConnectionError, OBSTimeoutError, OBSSourceError) as exc:
                outcome.failed.append(source)
                outcome.notes.append(f"恢复 {source} 失败：{exc}")
                outcome.snapshot_kept = True
                log.error("恢复音源 %s 失败：%s", source, exc)
                return outcome

            self._clear_locked()
            log.info("事件 %s 的音频状态恢复完成：%s", event_id, outcome.summary())
            return outcome

    def _restore_snapshot(
        self, snapshot: SourceSnapshot, applied: AppliedControl | None, outcome: RestoreOutcome
    ) -> None:
        source = snapshot.source
        current = self._control.get_state(source)
        restore_mute = True
        restore_volume = True

        if applied is not None:
            if applied.muted is not None and current.muted != applied.muted:
                restore_mute = False
                outcome.skipped_user_modified.append(f"{source}（静音状态已被手动修改）")
            if (
                applied.volume_mul is not None
                and abs(current.volume_mul - applied.volume_mul) > VOLUME_TOLERANCE
            ):
                restore_volume = False
                outcome.skipped_user_modified.append(f"{source}（音量已被手动修改）")

        if not restore_mute and not restore_volume:
            outcome.notes.append("音源状态已由用户接管，未做任何覆盖")
            return

        # 顺序很关键：如果原本是静音，先静音再改音量，避免中间瞬间漏音。
        if restore_mute and snapshot.muted:
            self._control.set_muted(source, True)
            if restore_volume:
                self._control.set_volume_mul(source, snapshot.volume_mul)
        else:
            if restore_volume:
                self._control.set_volume_mul(source, snapshot.volume_mul)
            if restore_mute:
                self._control.set_muted(source, snapshot.muted)

        restored_fields: list[str] = []
        if restore_mute:
            restored_fields.append(f"muted={snapshot.muted}")
        if restore_volume:
            restored_fields.append(f"volume={snapshot.volume_mul:.3f}")
        outcome.restored.append(f"{source}（{'，'.join(restored_fields)}）")

    def release(self) -> None:
        """放弃当前快照（用户确认不需要恢复时使用）。"""
        with self._lock:
            if self._event_id is not None:
                log.info("放弃音频快照（事件 %s）", self._event_id)
            self._clear_locked()

    def reset_to_snapshot(self) -> RestoreOutcome:
        """「重置音频状态」按钮：按最近一次快照恢复（忽略用户改动检测）。"""
        with self._lock:
            snapshot = self._snapshot
            if snapshot is None:
                return RestoreOutcome(notes=["没有可用的快照"])
            if not self._control.connected:
                return RestoreOutcome(
                    failed=[snapshot.source],
                    notes=["OBS 未连接，无法重置"],
                    snapshot_kept=True,
                )
            outcome = RestoreOutcome()
            try:
                if snapshot.muted:
                    self._control.set_muted(snapshot.source, True)
                self._control.set_volume_mul(snapshot.source, snapshot.volume_mul)
                if not snapshot.muted:
                    self._control.set_muted(snapshot.source, False)
                outcome.restored.append(f"{snapshot.source}（手动重置）")
            except Exception as exc:
                outcome.failed.append(snapshot.source)
                outcome.notes.append(f"重置失败：{exc}")
                outcome.snapshot_kept = True
                return outcome
            self._clear_locked()
            return outcome

    # -- 落盘（异常退出后的提示用） ----------------------------------------- #
    def _persist(self) -> None:
        if self._snapshot is None:
            return
        payload = {
            "event_id": self._event_id,
            "snapshot": self._snapshot.to_dict(),
            "applied": asdict(self._applied) if self._applied else None,
        }
        try:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._persist_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self._persist_path)
        except OSError as exc:  # pragma: no cover
            log.warning("写入音频快照失败：%s", exc)

    def _clear_persist(self) -> None:
        try:
            if self._persist_path.exists():
                self._persist_path.unlink()
        except OSError as exc:  # pragma: no cover
            log.warning("清理音频快照失败：%s", exc)

    def _clear_locked(self) -> None:
        self._event_id = None
        self._snapshot = None
        self._applied = None
        self._clear_persist()

    @staticmethod
    def load_pending(path: Path | None = None) -> dict[str, Any] | None:
        """启动时检查上次是否留下未恢复的音源状态。"""
        target = Path(path) if path else default_state_path()
        if not target.exists():
            return None
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) and data.get("snapshot") else None

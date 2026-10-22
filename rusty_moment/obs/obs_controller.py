"""OBS 音源的高层控制：查询、存在性校验、静音/音量设置。

只负责「跟 OBS 说话」，不负责记录/恢复状态——那是
:class:`rusty_moment.audio.audio_state_manager.AudioStateManager` 的职责。
这样两者都能单独测试。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from .obs_client import (
    OBSClient,
    OBSConnectionError,
    OBSRequestError,
    OBSTimeoutError,
)

log = logging.getLogger(__name__)

# 已知的音频设备输入类型（不需要额外探测就一定是音频源）
AUDIO_INPUT_KINDS = {
    "wasapi_output_capture",
    "wasapi_input_capture",
    "wasapi_process_output_capture",
    "coreaudio_input_capture",
    "coreaudio_output_capture",
    "pulse_input_capture",
    "pulse_output_capture",
    "alsa_input_capture",
    "jack_input_capture",
    "audio_line",
    "ffmpeg_source",
    "vlc_source",
    "media_source",
}


class OBSSourceError(Exception):
    """音源不存在 / 改名 / 不可控制。"""


@dataclass(frozen=True)
class InputInfo:
    name: str
    kind: str
    has_audio: bool


@dataclass(frozen=True)
class AudioSourceState:
    """某一时刻音源的可恢复状态。"""

    name: str
    muted: bool
    volume_mul: float

    def same_as(self, other: "AudioSourceState", tol: float = 1e-4) -> bool:
        return (
            self.name == other.name
            and self.muted == other.muted
            and abs(self.volume_mul - other.volume_mul) <= tol
        )


class OBSController:
    """对 :class:`OBSClient` 的语义化封装，所有方法都会做参数/存在性校验。"""

    def __init__(
        self,
        client: OBSClient,
        *,
        on_connection_change: Callable[[bool], None] | None = None,
    ) -> None:
        self.client = client
        self._on_connection_change = on_connection_change
        self._cache_inputs: list[InputInfo] | None = None

    # -- 连接 --------------------------------------------------------------- #
    @property
    def connected(self) -> bool:
        return self.client.connected

    def set_password_provider(self, provider: Callable[[], str | None]) -> None:
        self.client.set_password_provider(provider)

    @property
    def last_error(self) -> str:
        return self.client.last_error

    def connect(self) -> str:
        """连接并返回 OBS 版本描述，失败抛 OBSConnectionError。"""
        self.client.connect()
        self._cache_inputs = None
        version = self.query_version()
        if self._on_connection_change:
            self._on_connection_change(True)
        return version

    def disconnect(self) -> None:
        self.client.close()
        self._cache_inputs = None
        if self._on_connection_change:
            self._on_connection_change(False)

    def query_version(self) -> str:
        data = self.client.request("GetVersion")
        obs_version = str(data.get("obsVersion", "?"))
        ws_version = str(data.get("obsWebSocketVersion", "?"))
        return f"OBS {obs_version} / obs-websocket {ws_version}"

    def test_connection(self) -> str:
        """连接测试：连上后立刻查版本，再断开。"""
        was_connected = self.connected
        if not was_connected:
            self.client.connect()
        try:
            return self.query_version()
        finally:
            if not was_connected:
                self.client.close()

    # -- 音源查询 ----------------------------------------------------------- #
    def list_inputs(self, force_refresh: bool = False) -> list[InputInfo]:
        if self._cache_inputs is not None and not force_refresh:
            return self._cache_inputs
        data = self.client.request("GetInputList")
        raw_inputs = data.get("inputs", []) or []
        infos: list[InputInfo] = []
        for item in raw_inputs:
            name = str(item.get("inputName", ""))
            kind = str(item.get("inputKind") or item.get("unversionedInputKind") or "")
            if not name:
                continue
            has_audio = kind in AUDIO_INPUT_KINDS or self._has_audio_track(name)
            infos.append(InputInfo(name=name, kind=kind, has_audio=has_audio))
        self._cache_inputs = infos
        return infos

    def _has_audio_track(self, name: str) -> bool:
        try:
            data = self.client.request("GetInputAudioTracks", {"inputName": name}, timeout=2.0)
        except (OBSRequestError, OBSTimeoutError, OBSConnectionError):
            return False
        tracks = data.get("inputAudioTracks", {}) or {}
        return any(bool(value) for value in tracks.values())

    def list_audio_sources(self, force_refresh: bool = False) -> list[str]:
        return [info.name for info in self.list_inputs(force_refresh) if info.has_audio]

    def source_exists(self, name: str) -> bool:
        if not name:
            return False
        return any(info.name == name for info in self.list_inputs(force_refresh=True))

    def require_source(self, name: str) -> None:
        """音源必须存在，否则抛 :class:`OBSSourceError`（避免静默控制错误对象）。"""
        if not name:
            raise OBSSourceError("未配置音源名称")
        if not self.source_exists(name):
            raise OBSSourceError(f"OBS 中找不到音源「{name}」，可能已被改名或删除，请重新选择")

    # -- 状态读写 ----------------------------------------------------------- #
    def _request(self, request_type: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        """统一把「源不存在」映射成 :class:`OBSSourceError`，避免上层误判。"""
        try:
            return self.client.request(request_type, data)
        except OBSRequestError as exc:
            if exc.code in (600, 601):  # 600: 找不到源 / 601: 找不到分组
                self._cache_inputs = None
                name = (data or {}).get("inputName", "")
                raise OBSSourceError(
                    f"OBS 中找不到音源「{name}」，可能已被改名或删除，请重新选择"
                ) from exc
            raise

    def get_state(self, name: str) -> AudioSourceState:
        self.require_source(name)
        mute_data = self._request("GetInputMute", {"inputName": name})
        vol_data = self._request("GetInputVolume", {"inputName": name})
        return AudioSourceState(
            name=name,
            muted=bool(mute_data.get("inputMuted", False)),
            volume_mul=float(vol_data.get("inputVolumeMul", 1.0) or 0.0),
        )

    def set_muted(self, name: str, muted: bool) -> None:
        self._request("SetInputMute", {"inputName": name, "inputMuted": bool(muted)})

    def set_volume_mul(self, name: str, volume_mul: float) -> None:
        clamped = max(0.0, min(1.0, float(volume_mul)))
        self._request("SetInputVolume", {"inputName": name, "inputVolumeMul": clamped})

    # -- 复合操作 ----------------------------------------------------------- #
    def apply_control(self, name: str, strategy: str, duck_volume: float) -> None:
        """按策略切断游戏声音：``mute`` 直接静音，``duck`` 压低到指定比例。"""
        if strategy == "duck":
            self.set_volume_mul(name, duck_volume)
        else:
            self.set_muted(name, True)

    def raw_request(self, request_type: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.client.request(request_type, data)

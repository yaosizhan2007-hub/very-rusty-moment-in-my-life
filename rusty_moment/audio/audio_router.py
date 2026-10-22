"""系统级音频后端接口（默认未实现）。

第一版只控制 OBS 内部音源，**不声称**能切断 Windows 中任意游戏的声音。
如果将来要做系统级控制，请实现本接口，并先做技术验证：

- Windows 应用级音量：Core Audio ``ISimpleAudioVolume`` / ``IAudioSessionControl2``
  可以按 session 调节音量或静音，但对「进程没有音频会话」「多进程游戏」
  「独占/独占式 WASAPI 输出」并不可靠，且需要额外的 COM 封装与轮询。
- 虚拟音频设备（VB-Cable 等）：效果稳定，但要求用户安装内核驱动，
  需要明确提示安装、卸载与故障恢复方式。
- 音频路由/分轨：把游戏与 BGM 分到不同输出设备，OBS 分别采集。

在完成真机验证之前，任何界面文案都必须写明「仅控制 OBS 内部音源」。
"""

from __future__ import annotations


class SystemAudioBackend:
    """系统级音频控制的抽象接口。"""

    name = "unavailable"
    available = False

    def is_available(self) -> bool:
        return False

    def get_volume(self, process_name: str) -> float:
        raise NotImplementedError("系统级音频控制尚未实现")

    def set_muted(self, process_name: str, muted: bool) -> None:
        raise NotImplementedError("系统级音频控制尚未实现")

    def set_volume(self, process_name: str, volume: float) -> None:
        raise NotImplementedError("系统级音频控制尚未实现")

    def describe_limitations(self) -> str:
        return (
            "本版本仅通过 OBS WebSocket 控制 OBS 内部音源，不能直接切断 Windows 中"
            "游戏本身的声音。若需要在 OBS 之外的层面控制，请把游戏音频交给 OBS 采集"
            "（桌面音频 / 应用音频采集），或使用虚拟音频设备把游戏与 BGM 分轨。"
        )

"""OBS 控制器测试：音源枚举、存在性校验、静音/音量读写。"""

from __future__ import annotations

import pytest

from fakes import FakeInput, FakeOBS
from rusty_moment.obs.obs_client import OBSClient
from rusty_moment.obs.obs_controller import OBSController, OBSSourceError


def build(obs: FakeOBS) -> tuple[OBSClient, OBSController]:
    client = OBSClient(
        host="127.0.0.1",
        port=4455,
        password_provider=lambda: obs.password,
        transport_factory=obs.transport_factory(),
        timeout=1.0,
    )
    controller = OBSController(client)
    controller.connect()
    return client, controller


def test_list_audio_sources_filters_video_only_inputs():
    obs = FakeOBS()
    client, controller = build(obs)
    try:
        sources = controller.list_audio_sources()
        assert "桌面音频" in sources
        assert "麦克风" in sources
        assert "游戏捕获" not in sources  # 没有音轨的视频源不应出现在音频列表里
    finally:
        client.close()


def test_source_exists_and_require_source():
    obs = FakeOBS()
    client, controller = build(obs)
    try:
        assert controller.source_exists("桌面音频")
        assert not controller.source_exists("不存在")
        with pytest.raises(OBSSourceError):
            controller.require_source("不存在")
        with pytest.raises(OBSSourceError):
            controller.require_source("")
    finally:
        client.close()


def test_get_and_set_state_roundtrip():
    obs = FakeOBS(inputs=[FakeInput("桌面音频", "wasapi_output_capture", muted=False, volume=0.8)])
    client, controller = build(obs)
    try:
        state = controller.get_state("桌面音频")
        assert state.muted is False
        assert state.volume_mul == pytest.approx(0.8)

        controller.set_muted("桌面音频", True)
        controller.set_volume_mul("桌面音频", 0.15)
        state = controller.get_state("桌面音频")
        assert state.muted is True
        assert state.volume_mul == pytest.approx(0.15)
    finally:
        client.close()


def test_apply_control_mute_and_duck():
    obs = FakeOBS(inputs=[FakeInput("桌面音频", "wasapi_output_capture", volume=0.9)])
    client, controller = build(obs)
    try:
        controller.apply_control("桌面音频", "mute", 0.2)
        assert obs.input("桌面音频").muted is True
        assert obs.input("桌面音频").volume == pytest.approx(0.9)

        controller.apply_control("桌面音频", "duck", 0.2)
        assert obs.input("桌面音频").volume == pytest.approx(0.2)
    finally:
        client.close()


def test_set_muted_on_missing_source_raises():
    obs = FakeOBS()
    client, controller = build(obs)
    try:
        with pytest.raises(OBSSourceError):
            controller.set_muted("已改名音源", True)
    finally:
        client.close()


def test_query_version_text():
    obs = FakeOBS()
    client, controller = build(obs)
    try:
        assert "32.2.2" in controller.query_version()
    finally:
        client.close()

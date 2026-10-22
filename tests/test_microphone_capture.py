"""麦克风设备分类测试：区分真实麦克风与「系统输出回环」设备。

这个区分很关键：选了立体声混音/loopback 设备，就会把队友开麦说话也录进来。
"""

from __future__ import annotations

import numpy as np
import pytest

from rusty_moment.detection.microphone_capture import (
    MicrophoneCapture,
    MicrophoneError,
    is_loopback_device_name,
)


@pytest.mark.parametrize(
    "name",
    [
        "立体声混音 (Realtek HD Audio Stereo input)",
        "Stereo Mix (Realtek Audio)",
        "What U Hear",
        "电脑扬声器 (Realtek HD Audio output with SST)",
        "Speakers (Loopback)",
        "Monitor of Built-in Audio",
    ],
)
def test_loopback_names_are_detected(name):
    assert is_loopback_device_name(name) is True


@pytest.mark.parametrize(
    "name",
    [
        "麦克风阵列 (Realtek(R) Audio)",
        "麦克风 (Realtek HD Audio Mic input)",
        "Microphone (USB Audio Device)",
        "耳机 (@System32)\\drivers\\bthhfenum.sys",
        "",
    ],
)
def test_real_microphones_are_not_loopback(name):
    assert is_loopback_device_name(name) is False


class FakeStream:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.active = False

    def start(self) -> None:
        self.active = True

    def stop(self) -> None:
        self.active = False

    def close(self) -> None:
        self.active = False


class FakeInputSD:
    def __init__(self, names: list[tuple[str, int]]) -> None:
        self.devices = [
            {
                "name": name,
                "max_input_channels": channels,
                "max_output_channels": 0,
                "default_samplerate": 48000,
            }
            for name, channels in names
        ]

    def query_devices(self, index=None, kind=None):
        if kind == "input":
            return self.devices[0]
        if index is None:
            return self.devices
        return self.devices[index]

    def InputStream(self, **kwargs):
        return FakeStream(**kwargs)


def test_list_input_devices_marks_loopback():
    sd = FakeInputSD(
        [
            ("麦克风阵列 (Realtek(R) Audio)", 2),
            ("立体声混音 (Realtek HD Audio Stereo input)", 2),
            ("扬声器输出 (无输入通道)", 0),
        ]
    )
    capture = MicrophoneCapture(sd_module=sd)
    devices = capture.list_input_devices()
    assert [device.name for device in devices] == [
        "麦克风阵列 (Realtek(R) Audio)",
        "立体声混音 (Realtek HD Audio Stereo input)",
    ]
    assert devices[0].is_loopback is False
    assert devices[1].is_loopback is True


def test_start_records_device_name_and_loopback_flag():
    sd = FakeInputSD(
        [
            ("立体声混音 (Realtek HD Audio Stereo input)", 2),
            ("麦克风阵列 (Realtek(R) Audio)", 2),
        ]
    )
    capture = MicrophoneCapture(sd_module=sd)
    capture.start("1", samplerate=16000, blocksize=480)
    try:
        assert capture.is_running
        assert capture.device_name == "麦克风阵列 (Realtek(R) Audio)"
        assert capture.is_loopback is False
    finally:
        capture.stop()


def test_resolve_device_rejects_unknown_name():
    sd = FakeInputSD([("麦克风阵列 (Realtek(R) Audio)", 2)])
    capture = MicrophoneCapture(sd_module=sd)
    with pytest.raises(MicrophoneError):
        capture.resolve_device("不存在的麦克风")


def test_callback_pushes_into_queue():
    sd = FakeInputSD([("麦克风阵列 (Realtek(R) Audio)", 2)])
    capture = MicrophoneCapture(sd_module=sd, max_queue_blocks=2)
    block = np.zeros((480, 1), dtype=np.float32)
    capture._callback(block, 480, None, "")
    got = capture.read(timeout=0.1)
    assert got is not None and got.shape == (480,)

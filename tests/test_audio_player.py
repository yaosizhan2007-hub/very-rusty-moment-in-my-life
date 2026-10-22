"""播放器测试：使用 fake sounddevice / fake 解码器，避免依赖真实声卡与音频文件。"""

from __future__ import annotations

import numpy as np
import pytest

from rusty_moment.audio.audio_player import (
    AudioDeviceError,
    AudioFileError,
    AudioPlayer,
    PlaybackState,
)
from rusty_moment.audio.decoder import DecodeError, DecodedAudio


class FakeStream:
    def __init__(self, sd: "FakeSD", **kwargs) -> None:
        self.sd = sd
        self.kwargs = kwargs
        self.active = False
        self.closed = False

    def start(self) -> None:
        samplerate = self.kwargs["samplerate"]
        if samplerate not in self.sd.supported_rates:
            raise RuntimeError(f"Invalid sample rate {samplerate}")
        self.active = True

    def stop(self) -> None:
        self.active = False

    def close(self) -> None:
        self.active = False
        self.closed = True


class FakeSD:
    """最小 sounddevice 替身。"""

    class CallbackStop(Exception):
        pass

    def __init__(self, supported_rates: tuple[int, ...] = (48000,)) -> None:
        self.supported_rates = supported_rates
        self.streams: list[FakeStream] = []
        self.devices = [
            {"name": "Speakers (Realtek)", "max_input_channels": 0, "max_output_channels": 2, "default_samplerate": 48000},
            {"name": "Mic (USB)", "max_input_channels": 1, "max_output_channels": 0, "default_samplerate": 44100},
            {"name": "Headphones", "max_input_channels": 0, "max_output_channels": 2, "default_samplerate": 44100},
        ]

    def query_devices(self, index=None, kind=None):
        if kind == "output":
            return self.devices[0]
        if kind == "input":
            return self.devices[1]
        if index is None:
            return self.devices
        return self.devices[index]

    def OutputStream(self, **kwargs):
        stream = FakeStream(self, **kwargs)
        self.streams.append(stream)
        return stream


class FakeDecoder:
    """按帧数/采样率造一段音频，替代 libsndfile / PyAV。"""

    def __init__(
        self,
        frames: int = 4800,
        samplerate: int = 48000,
        channels: int = 2,
        *,
        error: Exception | None = None,
        format: str = "WAV",
        backend: str = "soundfile",
    ) -> None:
        self.frames = frames
        self.samplerate = samplerate
        self.channels = channels
        self.error = error
        self.format = format
        self.backend = backend
        self.calls: list[str] = []

    def __call__(self, path) -> DecodedAudio:
        self.calls.append(str(path))
        if self.error is not None:
            raise self.error
        data = np.zeros((self.frames, self.channels), dtype=np.float32)
        data[:, 0] = 0.1
        return DecodedAudio(
            samples=data,
            samplerate=self.samplerate,
            backend=self.backend,
            format=self.format,
        )


@pytest.fixture()
def media_file(tmp_path):
    path = tmp_path / "bgm.wav"
    path.write_bytes(b"RIFFfake")
    return path


def make_player(sd: FakeSD | None = None, decoder: FakeDecoder | None = None, **kwargs) -> AudioPlayer:
    return AudioPlayer(sd_module=sd or FakeSD(), decoder=decoder or FakeDecoder(), **kwargs)


def test_load_reports_duration_and_format(media_file):
    player = make_player()
    info = player.load(media_file)
    assert info.duration_s == pytest.approx(0.1)
    assert info.samplerate == 48000
    assert info.channels == 2
    assert info.format == "WAV"
    assert info.backend == "soundfile"


def test_load_accepts_aac(media_file):
    """AAC/M4A 交给 PyAV 后端，解码器返回什么播放器就用什么。"""
    aac = media_file.with_suffix(".aac")
    aac.write_bytes(b"fake-aac")
    decoder = FakeDecoder(format="AAC", backend="pyav")
    player = make_player(decoder=decoder)
    info = player.load(aac)
    assert info.format == "AAC"
    assert info.backend == "pyav"


def test_missing_file_raises(media_file, tmp_path):
    player = make_player()
    with pytest.raises(AudioFileError):
        player.load(tmp_path / "nope.wav")


def test_unsupported_extension_raises(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_bytes(b"x")
    player = make_player()
    with pytest.raises(AudioFileError):
        player.load(path)


def test_decoder_failure_is_wrapped(media_file):
    player = make_player(decoder=FakeDecoder(error=DecodeError("两个后端都失败")))
    with pytest.raises(AudioFileError) as excinfo:
        player.load(media_file)
    assert "无法解码" in str(excinfo.value)


def test_play_pause_resume_stop(media_file):
    sd = FakeSD()
    player = make_player(sd)
    player.play(media_file, volume=0.5)

    assert player.state == PlaybackState.PLAYING
    assert player.is_active

    player.pause()
    assert player.state == PlaybackState.PAUSED
    player.resume()
    assert player.state == PlaybackState.PLAYING

    player.stop()
    assert player.state == PlaybackState.IDLE
    assert not sd.streams[-1].active


def test_switch_track_reloads_audio(tmp_path):
    """切换曲目（不同文件）必须重新解码，而不是继续用旧缓冲。"""
    first = tmp_path / "a.wav"
    second = tmp_path / "b.wav"
    first.write_bytes(b"a")
    second.write_bytes(b"b")
    decoder = FakeDecoder(frames=1000)
    player = make_player(decoder=decoder)

    player.play(first)
    assert decoder.calls == [str(first)]
    player.play(second)
    assert decoder.calls == [str(first), str(second)]
    assert player.path == str(second)


def test_volume_is_clamped(media_file):
    player = make_player()
    player.play(media_file, volume=3.0)
    assert player.volume == 1.0
    player.set_volume(-1)
    assert player.volume == 0.0


def test_device_resolution_by_name_and_index():
    sd = FakeSD()
    player = make_player(sd)
    assert player.resolve_output_device("") is None
    assert player.resolve_output_device("Head") == 2
    with pytest.raises(AudioDeviceError):
        player.resolve_output_device("不存在设备")
    with pytest.raises(AudioDeviceError):
        player.resolve_output_device("1")  # 索引 1 只有输入通道


def test_sample_rate_fallback_resamples(media_file):
    decoder = FakeDecoder(frames=4410, samplerate=44100, channels=1)
    sd = FakeSD(supported_rates=(48000,))
    player = make_player(sd, decoder)
    player.play(media_file)
    stream = sd.streams[-1]
    assert stream.kwargs["samplerate"] == 48000
    assert player.duration == pytest.approx(0.1)


def test_callback_fills_buffer_and_stops_at_end(media_file):
    sd = FakeSD()
    player = make_player(sd, FakeDecoder(frames=1024, samplerate=48000, channels=2))
    player.play(media_file)
    stream = sd.streams[-1]
    outdata = np.ones((1024, 2), dtype=np.float32)
    stream.kwargs["callback"](outdata, 1024, None, "")
    assert np.allclose(outdata[:, 0], 0.1 * 0.8)

    outdata2 = np.ones((1024, 2), dtype=np.float32)
    with pytest.raises(FakeSD.CallbackStop):
        stream.kwargs["callback"](outdata2, 1024, None, "")


def test_close_releases_resources(media_file):
    sd = FakeSD()
    player = make_player(sd)
    player.play(media_file)
    player.close()
    assert player.info is None
    assert not sd.streams[-1].active


def test_no_track_selected_raises():
    player = make_player()
    with pytest.raises(AudioFileError):
        player.play(None)

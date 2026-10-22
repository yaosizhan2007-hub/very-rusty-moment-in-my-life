"""解码器测试：libsndfile 与 PyAV 双后端、失败处理、格式覆盖。"""

from __future__ import annotations

import numpy as np
import pytest
import soundfile as sf

from rusty_moment.audio.decoder import (
    ALL_SUPPORTED_SUFFIXES,
    DecodeError,
    decode_audio,
    decode_with_pyav,
    decode_with_soundfile,
    probe_duration,
)

SR = 22050


def write_wav(path, seconds: float = 0.5) -> None:
    n = int(SR * seconds)
    data = np.zeros((n, 2), dtype=np.float32)
    data[:, 0] = 0.2 * np.sin(2 * np.pi * 440 * np.arange(n) / SR)
    data[:, 1] = data[:, 0]
    sf.write(str(path), data, SR)


def test_soundfile_backend_for_wav(tmp_path):
    path = tmp_path / "tone.wav"
    write_wav(path)
    decoded = decode_audio(path)
    assert decoded.backend == "soundfile"
    assert decoded.samplerate == SR
    assert decoded.channels == 2
    assert decoded.duration_s == pytest.approx(0.5, abs=0.01)
    assert decoded.format == "WAV"


def test_mp3_roundtrip(tmp_path):
    path = tmp_path / "tone.mp3"
    write_wav(path.with_suffix(".wav"))
    data, sr = sf.read(str(path.with_suffix(".wav")), dtype="float32", always_2d=True)
    sf.write(str(path), data, sr, format="MP3")
    decoded = decode_audio(path)
    assert decoded.duration_s == pytest.approx(0.5, abs=0.1)
    assert decoded.samplerate == sr


def test_pyav_backend_decodes_mp4_aac(tmp_path):
    """用 PyAV 自己编码一段 AAC 再解回来，验证 FFmpeg 通路（真实 aac 容器）。"""
    av = pytest.importorskip("av")
    target = tmp_path / "tone.m4a"
    sr = 44100
    n = int(sr * 0.4)
    samples = (0.2 * np.sin(2 * np.pi * 440 * np.arange(n) / sr)).astype(np.float32)

    with av.open(str(target), mode="w") as container:
        stream = container.add_stream("aac", rate=sr)
        stream.layout = "stereo"
        frame = av.AudioFrame(format="fltp", layout="stereo", samples=n)
        frame.sample_rate = sr
        for plane in range(2):
            frame.planes[plane].update(samples.tobytes())
        for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)

    decoded = decode_with_pyav(target)
    assert decoded.backend == "pyav"
    assert decoded.channels == 2
    assert decoded.duration_s == pytest.approx(0.4, abs=0.1)
    assert decoded.format == "M4A"


def test_corrupt_file_raises_decode_error(tmp_path):
    broken = tmp_path / "broken.aac"
    broken.write_bytes(b"\x00\x01\x02not audio")
    with pytest.raises(DecodeError):
        decode_audio(broken)


def test_empty_file_raises(tmp_path):
    empty = tmp_path / "empty.wav"
    empty.write_bytes(b"")
    with pytest.raises(DecodeError):
        decode_audio(empty)


def test_missing_file_raises(tmp_path):
    with pytest.raises(DecodeError):
        decode_audio(tmp_path / "nope.wav")


def test_unsupported_suffix_falls_back_and_reports_error(tmp_path):
    weird = tmp_path / "weird.xyz"
    weird.write_bytes(b"whatever")
    with pytest.raises(DecodeError):
        decode_audio(weird)


def test_probe_duration_is_safe_on_bad_file(tmp_path):
    bad = tmp_path / "bad.mp3"
    bad.write_bytes(b"nope")
    assert probe_duration(bad) == 0.0


def test_soundfile_backend_function_direct(tmp_path):
    path = tmp_path / "direct.wav"
    write_wav(path, seconds=0.2)
    decoded = decode_with_soundfile(path)
    assert decoded.samples.shape[1] == 2
    assert decoded.samples.dtype == np.float32


def test_supported_suffixes_include_common_formats():
    for suffix in (".wav", ".mp3", ".flac", ".ogg", ".aac", ".m4a"):
        assert suffix in ALL_SUPPORTED_SUFFIXES

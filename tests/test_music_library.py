"""音乐库测试：默认曲目发现、标题清洗、路径过滤。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import soundfile as sf

from rusty_moment.audio.music_library import (
    discover_tracks,
    iter_audio_files,
    make_track,
    music_directory,
    track_title,
)


def write_silent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), np.zeros((800, 2), dtype=np.float32), 8000)


def test_music_directory_exists():
    directory = music_directory()
    assert isinstance(directory, Path)
    assert directory.name.lower() in ("music", "音乐") or directory.exists()


def test_track_title_prefers_bracket_name():
    title = track_title("影视原声 P1 《示例合集》插曲「A Rusty Dream」高音质试听_41990000000.aac")
    assert title == "A Rusty Dream"


def test_track_title_strips_trailing_id():
    title = track_title("影视原声 P1 《示例合集》片头曲_37200000000.aac")
    assert "_37200000000" not in title
    assert "影视原声" not in title


def test_track_title_truncates_long_names():
    title = track_title("a" * 100 + ".mp3")
    assert len(title) <= 42


def test_track_title_falls_back_to_stem():
    assert track_title("simple_song.wav") == "simple_song"


def test_iter_audio_files_filters_by_suffix(tmp_path):
    write_silent(tmp_path / "a.wav")
    write_silent(tmp_path / "sub" / "b.mp3")
    (tmp_path / "note.txt").write_text("x", encoding="utf-8")
    found = list(iter_audio_files(tmp_path))
    assert {path.name for path in found} == {"a.wav", "b.mp3"}


def test_discover_returns_at_most_three(tmp_path):
    for index in range(6):
        write_silent(tmp_path / f"track{index}.wav")
    tracks = discover_tracks(limit=3, directory=tmp_path)
    assert len(tracks) == 3
    assert all(track["path"].endswith(".wav") for track in tracks)
    assert all(track["title"] for track in tracks)


def test_discover_missing_directory_returns_empty(tmp_path):
    assert discover_tracks(directory=tmp_path / "nope") == []


def test_make_track_uses_custom_title(tmp_path):
    path = tmp_path / "x.wav"
    write_silent(path)
    track = make_track(path, "自定义标题")
    assert track["title"] == "自定义标题"
    assert track["path"] == str(path)

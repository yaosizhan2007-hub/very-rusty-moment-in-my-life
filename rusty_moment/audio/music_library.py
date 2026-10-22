"""本地音乐库：扫描音乐目录、生成曲目列表、清洗显示名。

「默认三首曲子」的实现：首次启动（或配置里没有曲目时）扫描 Windows 音乐目录，
最多取前三首受支持的音频。目录用系统 Known Folder（FOLDERID_Music）而不是写死
路径，换机器/换用户名也能用。
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path
from typing import Iterable

from .decoder import ALL_SUPPORTED_SUFFIXES

log = logging.getLogger(__name__)

DEFAULT_TRACK_LIMIT = 3
SCAN_DEPTH = 2

_TRAILING_ID = re.compile(r"[_\-\s]+\d{6,}$")
_BRACKET_TITLE = re.compile(r"[「『]([^」』]{1,60})[」』]")
_NOISE_PREFIX = re.compile(r"^影视原声\s*P?\d*\s*", re.IGNORECASE)


def music_directory() -> Path:
    """Windows「音乐」库目录；拿不到就退回 ~/Music。"""
    if sys.platform == "win32" and hasattr(sys, "getwindowsversion"):
        try:
            import ctypes
            from ctypes import wintypes

            # FOLDERID_Music = {4BD8D571-6D19-48D3-BE97-422220080E43}
            guid_bytes = (
                b"\x71\xd5\xd8\x4b\x19\x6d\xd3\x48\xbe\x97\x42\x22\x20\x08\x0e\x43"
            )

            class GUID(ctypes.Structure):
                _fields_ = [
                    ("Data1", wintypes.DWORD),
                    ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD),
                    ("Data4", ctypes.c_ubyte * 8),
                ]

            guid = GUID.from_buffer_copy(guid_bytes)
            path_ptr = ctypes.c_wchar_p()
            result = ctypes.windll.shell32.SHGetKnownFolderPath(  # type: ignore[attr-defined]
                ctypes.byref(guid), 0, None, ctypes.byref(path_ptr)
            )
            if result == 0 and path_ptr.value:
                return Path(path_ptr.value)
        except Exception as exc:  # pragma: no cover - 极端环境
            log.debug("读取系统音乐目录失败，退回 ~/Music：%s", exc)
    return Path.home() / "Music"


def track_title(path: str | Path) -> str:
    """从文件名提炼一个短标题（优先取「」里的曲名）。"""
    stem = Path(path).stem
    match = _BRACKET_TITLE.search(stem)
    if match:
        return match.group(1).strip()
    cleaned = _TRAILING_ID.sub("", _NOISE_PREFIX.sub("", stem)).strip()
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    if len(cleaned) > 42:
        cleaned = cleaned[:41].rstrip() + "…"
    return cleaned or Path(path).stem[:42]


def iter_audio_files(directory: Path, depth: int = SCAN_DEPTH) -> Iterable[Path]:
    base = Path(directory)
    if not base.exists():
        return []
    found: list[Path] = []
    try:
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix.lower() not in ALL_SUPPORTED_SUFFIXES:
                continue
            try:
                if len(path.relative_to(base).parts) > depth:
                    continue
            except ValueError:  # pragma: no cover
                continue
            found.append(path)
    except OSError as exc:  # pragma: no cover - 权限问题
        log.warning("扫描音乐目录失败：%s", exc)
    return sorted(found, key=lambda item: item.name.lower())


def discover_tracks(
    limit: int = DEFAULT_TRACK_LIMIT, directory: Path | None = None
) -> list[dict[str, str]]:
    """扫描音乐目录，返回最多 ``limit`` 条 ``{path, title}``。"""
    base = Path(directory) if directory else music_directory()
    tracks: list[dict[str, str]] = []
    for path in iter_audio_files(base):
        tracks.append({"path": str(path), "title": track_title(path)})
        if len(tracks) >= max(1, limit):
            break
    if tracks:
        log.info("从 %s 发现 %d 首默认曲目", base, len(tracks))
    return tracks


def make_track(path: str | Path, title: str = "") -> dict[str, str]:
    file_path = Path(path)
    return {"path": str(file_path), "title": (title or track_title(file_path)).strip()}

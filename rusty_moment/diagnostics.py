"""自检 / 诊断：一条命令验证运行环境、解码后端、依赖与曲目。

用途：
- 打包后的 exe 用 ``RustyMoment.exe --check`` 判断这台机器能不能放 AAC、能不能听语音；
- 用户报「放不出声」「识别不了」时，先让他跑这个，把输出发过来。

只做只读检查，不写配置、不改设备状态。
"""

from __future__ import annotations

import platform
import sys
from pathlib import Path

from . import __version__


def _line(ok: bool | None, label: str, detail: str = "") -> str:
    mark = "✅" if ok else ("⚠" if ok is None else "❌")
    return f"{mark} {label}" + (f"：{detail}" if detail else "")


def check_python() -> list[str]:
    lines = [
        _line(True, "Rusty Moment", __version__),
        _line(True, "Python", f"{platform.python_version()} ({sys.executable})"),
        _line(True, "系统", f"{platform.system()} {platform.release()}"),
        _line(True, "打包运行", "是" if getattr(sys, "frozen", False) else "否（源码运行）"),
    ]
    return lines


def check_audio_backends() -> list[str]:
    lines: list[str] = []
    try:
        import sounddevice as sd

        devices = sd.query_devices()
        outputs = sum(1 for device in devices if int(device.get("max_output_channels", 0)) > 0)
        inputs = sum(1 for device in devices if int(device.get("max_input_channels", 0)) > 0)
        lines.append(_line(True, "sounddevice", f"{sd.__version__}（输出设备 {outputs}，输入设备 {inputs}）"))
    except Exception as exc:
        lines.append(_line(False, "sounddevice", str(exc)))

    try:
        import soundfile as sf

        formats = ", ".join(
            name for name in ("WAV", "MP3", "FLAC", "OGG") if name in sf.available_formats()
        )
        lines.append(_line(True, "libsndfile", f"{sf.__libsndfile_version__}（{formats or '无'}）"))
    except Exception as exc:
        lines.append(_line(False, "libsndfile", str(exc)))

    try:
        import av

        lines.append(_line(True, "PyAV（AAC/M4A）", str(av.__version__)))
    except Exception as exc:
        lines.append(_line(None, "PyAV（AAC/M4A 不可用）", str(exc)))
    return lines


def check_optional_features() -> list[str]:
    lines: list[str] = []
    try:
        import faster_whisper

        lines.append(_line(True, "faster-whisper（语音识别）", str(faster_whisper.__version__)))
    except Exception as exc:
        lines.append(_line(None, "faster-whisper（语音识别不可用）", str(exc)))

    try:
        import httpx

        lines.append(_line(True, "httpx（AI 语义层）", httpx.__version__))
    except Exception as exc:
        lines.append(_line(None, "httpx（AI 语义层不可用）", str(exc)))

    try:
        from .security.secret_store import SecretStore

        store = SecretStore()
        lines.append(
            _line(store.available, "DPAPI 凭据存储", "可用" if store.available else store.unavailable_reason)
        )
    except Exception as exc:  # pragma: no cover
        lines.append(_line(False, "DPAPI 凭据存储", str(exc)))
    return lines


def check_library(path: Path | None = None, decode: bool = True) -> list[str]:
    from .config.config_manager import ConfigManager

    manager = ConfigManager(config_path=path) if path else ConfigManager()
    cfg = manager.load()
    tracks = cfg.audio.playlist
    if not tracks:
        return [_line(None, "曲目列表", "空（可在主界面「添加音乐…」）")]

    lines = [_line(True, "曲目列表", f"{len(tracks)} 首，当前第 {cfg.audio.current_index + 1} 首")]
    for index, track in enumerate(tracks):
        file_path = Path(str(track.get("path", "")))
        title = str(track.get("title", "")) or file_path.stem
        prefix = "▶" if index == cfg.audio.current_index else " "
        if not file_path.exists():
            lines.append(f"  {prefix} ❌ {title}：文件不存在 {file_path}")
            continue
        if not decode:
            lines.append(f"  {prefix} ✅ {title}")
            continue
        try:
            from .audio.decoder import decode_audio

            decoded = decode_audio(file_path)
            lines.append(
                f"  {prefix} ✅ {title}：{decoded.duration_s:.1f}s / {decoded.samplerate}Hz / "
                f"{decoded.format} / 后端 {decoded.backend}"
            )
        except Exception as exc:
            lines.append(f"  {prefix} ❌ {title}：{exc}")
    return lines


def run_checks(check_tracks: bool = True, decode: bool = True) -> int:
    """打印自检结果（并写一份到日志目录），返回进程退出码（0 = 关键项全通过）。

    打包成 windowed exe 后没有控制台，所以必须同时落盘：
    ``%APPDATA%\\RustyMoment\\logs\\self-check.txt``。
    """
    sections = [
        ("环境", check_python()),
        ("音频后端", check_audio_backends()),
        ("可选功能", check_optional_features()),
    ]
    if check_tracks:
        sections.append(("音乐库", check_library(decode=decode)))

    critical_failed = 0
    lines_out: list[str] = []
    for title, lines in sections:
        lines_out.append(f"[{title}]")
        for line in lines:
            lines_out.append("  " + line)
            if line.strip().startswith("❌") and title in ("环境", "音频后端"):
                critical_failed += 1
        lines_out.append("")
    lines_out.append("结论：" + ("关键项全部通过" if critical_failed == 0 else f"有 {critical_failed} 个关键项失败"))

    text = "\n".join(lines_out)
    # 先落盘再打印：Windows 控制台是 GBK，打印 ✅ 可能抛异常，
    # 但自检结果不能因此丢失。
    saved: Path | None = None
    try:
        from .utils.logging_setup import default_log_dir

        target = default_log_dir() / "self-check.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        saved = target
    except Exception:  # pragma: no cover - 写文件失败不影响退出码
        pass

    try:
        print(text)
        if saved is not None:
            print(f"\n（自检结果已写入 {saved}）")
    except Exception as exc:  # noqa: BLE001 - 控制台编码问题不该影响退出码
        detail = f"控制台输出失败（{exc}）"
        if saved is not None:
            detail += f"，结果已写入 {saved}"
        try:
            sys.stderr.write(detail + "\n")
        except Exception:  # pragma: no cover
            pass
    return 0 if critical_failed == 0 else 1

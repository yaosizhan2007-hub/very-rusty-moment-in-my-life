"""性能探针：给「这台机器跑起来到底卡不卡」一个可复现的答案。

不需要 psutil（避免额外依赖与打包体积）：用 Windows 自带的
``GetProcessMemoryInfo`` / ``GetProcessTimes`` 采样自身 CPU 与内存。
每一项都是**可选**的：设备不在、模型没下载、OBS 没开、Ollama 没运行，
就明确标注「跳过」，而不是编个数字。

用法：
    python main.py --perf                 # 默认测空闲/麦克风/切句/播放
    python main.py --perf --no-mic        # 不占用麦克风
    RustyMoment.exe --perf                # 打包后同样可用（结果写文件）
"""

from __future__ import annotations

import ctypes
import gc
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:  # pragma: no cover - 平台相关
    from ctypes import wintypes

    class _MEMORY_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    _HAS_WIN32 = sys.platform == "win32"
except Exception:  # pragma: no cover
    _HAS_WIN32 = False


@dataclass
class PerfRow:
    item: str
    detail: str
    metrics: dict[str, Any] = field(default_factory=dict)

    def format_metrics(self) -> str:
        return "  ".join(f"{key}={value}" for key, value in self.metrics.items())


class ProcessStats:
    """读取当前进程的内存与 CPU 时间（Windows）。"""

    def __init__(self) -> None:
        self._kernel32 = ctypes.windll.kernel32 if _HAS_WIN32 else None
        self._psapi = ctypes.windll.psapi if _HAS_WIN32 else None
        self._handle = None
        if not _HAS_WIN32:
            return
        try:
            from ctypes import wintypes

            # 不声明 argtypes/restype 时，64 位 Python 会把句柄/指针截断，调用直接失败
            self._psapi.GetProcessMemoryInfo.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(_MEMORY_COUNTERS),
                wintypes.DWORD,
            ]
            self._psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
            self._kernel32.GetProcessTimes.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
            ]
            self._kernel32.GetProcessTimes.restype = wintypes.BOOL
            self._kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            self._handle = self._kernel32.GetCurrentProcess()
        except Exception:  # pragma: no cover - 极端环境
            self._handle = None

    @property
    def available(self) -> bool:
        return bool(_HAS_WIN32 and self._handle)

    def memory_mb(self) -> float:
        if not self.available:
            return 0.0
        counters = _MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(counters)
        if not self._psapi.GetProcessMemoryInfo(
            self._handle, ctypes.byref(counters), counters.cb
        ):
            return 0.0
        return counters.WorkingSetSize / (1024 * 1024)

    def cpu_seconds(self) -> float:
        if not self.available:
            return 0.0
        creation, exit_, kernel, user = (
            wintypes.FILETIME(),
            wintypes.FILETIME(),
            wintypes.FILETIME(),
            wintypes.FILETIME(),
        )
        if not self._kernel32.GetProcessTimes(
            self._handle,
            ctypes.byref(creation),
            ctypes.byref(exit_),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return 0.0

        def _to_seconds(value: Any) -> float:
            ticks = (value.dwHighDateTime << 32) | value.dwLowDateTime
            return ticks / 1e7

        return _to_seconds(kernel) + _to_seconds(user)

    def sample_cpu_percent(self, window: float = 1.0) -> float:
        """在 window 秒内测平均 CPU 占用（占单核百分比，多核会 > 100%）。"""
        if not self.available:
            return 0.0
        start_cpu, start_wall = self.cpu_seconds(), time.perf_counter()
        time.sleep(window)
        elapsed_cpu = self.cpu_seconds() - start_cpu
        elapsed_wall = time.perf_counter() - start_wall
        if elapsed_wall <= 0:
            return 0.0
        return max(0.0, elapsed_cpu / elapsed_wall * 100.0)


def measure_idle(stats: ProcessStats, seconds: float = 3.0) -> PerfRow:
    gc.collect()
    baseline_mem = stats.memory_mb()
    cpu = stats.sample_cpu_percent(seconds)
    peak_mem = stats.memory_mb()
    return PerfRow(
        item="空闲",
        detail=f"{seconds:.0f}s 无操作",
        metrics={"CPU": f"{cpu:.1f}%", "内存": f"{baseline_mem:.1f}MB", "峰值": f"{peak_mem:.1f}MB"},
    )


def measure_microphone(stats: ProcessStats, seconds: float = 5.0) -> PerfRow:
    from .detection.microphone_capture import MicrophoneCapture, MicrophoneError
    from .detection.vad import VadConfig, VoiceSegmenter

    capture = MicrophoneCapture()
    try:
        capture.start(None, samplerate=16000, blocksize=480)
    except MicrophoneError as exc:
        return PerfRow(item="麦克风采集", detail=f"跳过：{exc}")

    segmenter = VoiceSegmenter(16000, VadConfig())
    blocks = 0
    cpu_samples: list[float] = []
    mem_before = stats.memory_mb()
    start_cpu = stats.cpu_seconds()
    start_wall = time.perf_counter()
    try:
        while time.perf_counter() - start_wall < seconds:
            block = capture.read(timeout=0.2)
            if block is None:
                continue
            blocks += 1
            segmenter.push(block)
            if blocks % 20 == 0:
                cpu_samples.append(stats.cpu_seconds())
    finally:
        capture.stop()

    wall = max(1e-6, time.perf_counter() - start_wall)
    cpu_percent = (stats.cpu_seconds() - start_cpu) / wall * 100.0
    mem_after = stats.memory_mb()
    return PerfRow(
        item="麦克风采集 + VAD",
        detail=f"{wall:.1f}s，{blocks} 块，丢弃 {capture.dropped_blocks}",
        metrics={
            "CPU": f"{cpu_percent:.1f}%",
            "内存增量": f"{mem_after - mem_before:+.1f}MB",
            "总内存": f"{mem_after:.1f}MB",
        },
    )


def measure_vad_offline() -> PerfRow:
    """纯 VAD 切句耗时（不依赖设备）。"""
    import numpy as np

    from rusty_moment.detection.vad import segment_offline, synth_speech_like

    sr = 16000
    audio = np.concatenate(
        [np.zeros(sr // 2, np.float32), synth_speech_like(3.0, sr), np.zeros(sr, np.float32)]
    )
    timings: list[float] = []
    for _ in range(5):
        t0 = time.perf_counter()
        utterances = segment_offline(audio, sr)
        timings.append((time.perf_counter() - t0) * 1000)
    audio_seconds = audio.shape[0] / sr
    mean_ms = statistics.mean(timings)
    return PerfRow(
        item="VAD 切句",
        detail=f"{audio_seconds:.1f}s 音频 × 5 次，切出 {len(utterances)} 段",
        metrics={
            "单次": f"{mean_ms:.1f}ms",
            "实时倍率": f"{audio_seconds * 1000 / mean_ms:.0f}×",
        },
    )


def measure_stt(model_size: str = "tiny") -> PerfRow:
    from .detection.speech_recognizer import FasterWhisperRecognizer, RecognizerError
    import numpy as np

    recognizer = FasterWhisperRecognizer(model_size=model_size, language="zh")
    t0 = time.perf_counter()
    try:
        recognizer.warmup()
    except RecognizerError as exc:
        return PerfRow(item="语音识别", detail=f"跳过：{exc}")
    load_ms = (time.perf_counter() - t0) * 1000

    # 1.5 秒近似语音（噪声+调制），只测耗时，不关心识别内容
    sr = 16000
    rng = np.random.default_rng(0)
    pcm = (rng.standard_normal(int(sr * 1.5)) * 0.05).astype(np.float32)
    timings: list[float] = []
    try:
        for _ in range(2):
            t1 = time.perf_counter()
            recognizer.transcribe(pcm, sr)
            timings.append((time.perf_counter() - t1) * 1000)
    except RecognizerError as exc:
        return PerfRow(item="语音识别", detail=f"跳过：{exc}")

    return PerfRow(
        item=f"语音识别（{model_size}）",
        detail="1.5s 音频 × 2 次",
        metrics={"加载": f"{load_ms:.0f}ms", "单句": f"{statistics.mean(timings):.0f}ms"},
    )


def measure_ai() -> PerfRow:
    """本机 Ollama 可用时测一次真实分析延迟。"""
    from .detection.ai_backends import LocalLLMAnalyzer
    from .detection.emotion_analyzer import AnalysisError

    analyzer = LocalLLMAnalyzer(model="qwen3:8b", timeout=60.0)
    t0 = time.perf_counter()
    try:
        analyzer.analyze("兄弟，别管我，你们快走，我来拖住他们。")
    except AnalysisError as exc:
        return PerfRow(item="AI 分析", detail=f"跳过：{exc}")
    elapsed = (time.perf_counter() - t0) * 1000
    return PerfRow(item="AI 分析（qwen3:8b）", detail="单句", metrics={"延迟": f"{elapsed:.0f}ms"})


def measure_playback_latency() -> PerfRow:
    """从调用 play() 到音频流真正 active 的时间。"""
    import tempfile

    import numpy as np
    import soundfile as sf

    from .audio.audio_player import AudioPlayer, AudioPlayerError

    player = AudioPlayer()
    path = Path(tempfile.gettempdir()) / "rm-perf-tone.wav"
    sf.write(str(path), np.zeros((24000, 2), dtype=np.float32), 48000)
    try:
        timings: list[float] = []
        for _ in range(3):
            t0 = time.perf_counter()
            player.play(path, volume=0.0)
            timings.append((time.perf_counter() - t0) * 1000)
            player.stop()
    except AudioPlayerError as exc:
        return PerfRow(item="播放启动", detail=f"跳过：{exc}")
    finally:
        player.close()
        try:
            path.unlink()
        except OSError:
            pass
    return PerfRow(
        item="播放启动（触发→出声）",
        detail="打开解码+流×3",
        metrics={"平均": f"{statistics.mean(timings):.0f}ms", "最慢": f"{max(timings):.0f}ms"},
    )


def measure_obs() -> PerfRow:
    """OBS 请求往返耗时（需要 OBS 在运行且已保存密码）。"""
    from .app import OBS_PASSWORD_KEY
    from .obs.obs_client import OBSClient
    from .obs.obs_controller import OBSController
    from .security.secret_store import SecretStore

    password = ""
    try:
        password = SecretStore().get_secret(OBS_PASSWORD_KEY) or ""
    except Exception:  # pragma: no cover
        password = ""
    client = OBSClient(password_provider=lambda: password, timeout=3.0)
    controller = OBSController(client)
    try:
        t0 = time.perf_counter()
        controller.connect()
        connect_ms = (time.perf_counter() - t0) * 1000
        t1 = time.perf_counter()
        controller.query_version()
        version_ms = (time.perf_counter() - t1) * 1000
        timings: list[float] = []
        for _ in range(5):
            t2 = time.perf_counter()
            controller.client.request("GetVersion")
            timings.append((time.perf_counter() - t2) * 1000)
    except Exception as exc:
        return PerfRow(item="OBS 控制", detail=f"跳过：{exc}")
    finally:
        try:
            controller.disconnect()
        except Exception:  # pragma: no cover
            pass
    return PerfRow(
        item="OBS 控制往返",
        detail="GetVersion ×5",
        metrics={
            "连接": f"{connect_ms:.0f}ms",
            "首次请求": f"{version_ms:.0f}ms",
            "平均往返": f"{statistics.mean(timings):.1f}ms",
        },
    )


def run_perf(
    *,
    seconds: float = 3.0,
    include_mic: bool = True,
    include_stt: bool = True,
    include_ai: bool = True,
    include_obs: bool = True,
) -> int:
    stats = ProcessStats()
    rows: list[PerfRow] = []
    rows.append(measure_idle(stats, seconds))
    rows.append(measure_vad_offline())
    rows.append(measure_playback_latency())
    if include_mic:
        rows.append(measure_microphone(stats))
    if include_stt:
        rows.append(measure_stt())
    if include_ai:
        rows.append(measure_ai())
    if include_obs:
        rows.append(measure_obs())

    lines = ["# Rusty Moment 性能实测", "", f"测量时间：{time.strftime('%Y-%m-%d %H:%M:%S')}", ""]
    for row in rows:
        print(f"{row.item:24s} {row.detail:42s} {row.format_metrics()}")
        lines.append(f"- **{row.item}**：{row.format_metrics()}　_{row.detail}_")
    if not stats.available:
        lines.append("")
        lines.append("（当前平台不支持进程采样，内存/CPU 指标为 0）")
        print("提示：进程 CPU/内存采样仅支持 Windows。")

    text = "\n".join(lines)
    try:
        from .utils.logging_setup import default_log_dir

        target = default_log_dir() / "perf-report.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        print(f"\n报告已写入 {target}")
    except Exception:  # pragma: no cover
        pass
    return 0

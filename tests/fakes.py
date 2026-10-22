"""测试替身：模拟 OBS WebSocket 服务端、OBS 控制器、播放器。

这些替身只实现项目真正用到的协议子集，但握手/认证/请求-响应/事件的行为与
obs-websocket 5.x 一致，因此被测代码不需要为测试改动。
"""

from __future__ import annotations

import base64
import hashlib
import json
import queue
import threading
import time
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from rusty_moment.obs.obs_controller import AudioSourceState, OBSSourceError


def obs_auth_string(password: str, salt: str, challenge: str) -> str:
    secret = base64.b64encode(hashlib.sha256((password + salt).encode("utf-8")).digest())
    return base64.b64encode(hashlib.sha256(secret + challenge.encode("utf-8")).digest()).decode()


def _ok(data: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    return {"result": True, "code": 100, "comment": None}, data or {}


def _fail(code: int, comment: str) -> tuple[dict[str, Any], dict[str, Any]]:
    return {"result": False, "code": code, "comment": comment}, {}


@dataclass
class FakeInput:
    name: str
    kind: str = "wasapi_output_capture"
    muted: bool = False
    volume: float = 1.0
    audio: bool = True


class FakeOBS:
    """模拟一个 obs-websocket 服务器。"""

    def __init__(
        self,
        *,
        password: str = "s3cret",
        require_auth: bool = True,
        inputs: list[FakeInput] | None = None,
    ) -> None:
        self.password = password
        self.require_auth = require_auth
        self.inputs: dict[str, FakeInput] = {
            item.name: item
            for item in (
                inputs
                if inputs is not None
                else [
                    FakeInput("桌面音频", "wasapi_output_capture"),
                    FakeInput("麦克风", "wasapi_input_capture", volume=0.4),
                    FakeInput("游戏捕获", "game_capture", audio=False),
                ]
            )
        }
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.drop_requests = False
        self.transports: list[FakeTransport] = []
        self.identify_count = 0
        self.auth_failures = 0

    # -- 工厂 --------------------------------------------------------------- #
    def transport_factory(self):
        def make() -> "FakeTransport":
            transport = FakeTransport(self)
            self.transports.append(transport)
            return transport

        return make

    # -- 状态 --------------------------------------------------------------- #
    def input(self, name: str) -> FakeInput:
        if name not in self.inputs:
            raise KeyError(name)
        return self.inputs[name]

    def disconnect_all(self) -> None:
        for transport in self.transports:
            transport.server_close()

    # -- 请求处理 ----------------------------------------------------------- #
    def handle_request(
        self, request_type: str, data: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if request_type == "GetVersion":
            return _ok({"obsVersion": "32.2.2", "obsWebSocketVersion": "5.5.6"})
        if request_type == "GetInputList":
            return _ok(
                {
                    "inputs": [
                        {"inputName": item.name, "inputKind": item.kind}
                        for item in self.inputs.values()
                    ]
                }
            )
        if request_type == "GetInputAudioTracks":
            item = self.inputs.get(str(data.get("inputName", "")))
            if item is None:
                return _fail(600, "No source was found by that name")
            return _ok({"inputAudioTracks": {str(i): item.audio for i in range(1, 7)}})
        if request_type == "GetInputMute":
            item = self.inputs.get(str(data.get("inputName", "")))
            if item is None:
                return _fail(600, "No source was found by that name")
            return _ok({"inputMuted": item.muted})
        if request_type == "SetInputMute":
            item = self.inputs.get(str(data.get("inputName", "")))
            if item is None:
                return _fail(600, "No source was found by that name")
            item.muted = bool(data.get("inputMuted"))
            return _ok()
        if request_type == "GetInputVolume":
            item = self.inputs.get(str(data.get("inputName", "")))
            if item is None:
                return _fail(600, "No source was found by that name")
            return _ok({"inputVolumeMul": item.volume, "inputVolumeDb": -6.0})
        if request_type == "SetInputVolume":
            item = self.inputs.get(str(data.get("inputName", "")))
            if item is None:
                return _fail(600, "No source was found by that name")
            item.volume = float(data.get("inputVolumeMul", 1.0))
            return _ok()
        return _fail(204, f"unknown request type {request_type}")


class FakeTransport:
    """按 obs-websocket 5.x 的 opcode 应答的内存传输层。"""

    def __init__(self, obs: FakeOBS) -> None:
        self.obs = obs
        self.salt = "test-salt"
        self.challenge = "test-challenge"
        self._incoming: queue.Queue[str | None] = queue.Queue()
        self._closed = False

    # -- Transport 协议 ------------------------------------------------------ #
    def connect(self, url: str, timeout: float) -> None:
        self.url = url
        hello: dict[str, Any] = {
            "op": 0,
            "d": {"obsWebSocketVersion": "5.5.6", "rpcVersion": 1},
        }
        if self.obs.require_auth:
            hello["d"]["authentication"] = {"challenge": self.challenge, "salt": self.salt}
        self._push(hello)

    def send(self, text: str) -> None:
        if self._closed:
            raise ConnectionError("transport closed")
        message = json.loads(text)
        op = message.get("op")
        if op == 1:
            data = message.get("d", {})
            if self.obs.require_auth:
                expected = obs_auth_string(self.obs.password, self.salt, self.challenge)
                if data.get("authentication") != expected:
                    self.obs.auth_failures += 1
                    self.server_close()
                    return
            self.obs.identify_count += 1
            self._push({"op": 2, "d": {"negotiatedRpcVersion": 1}})
        elif op == 6:
            payload = message.get("d", {})
            request_type = str(payload.get("requestType", ""))
            request_data = payload.get("requestData", {}) or {}
            self.obs.requests.append((request_type, request_data))
            if self.obs.drop_requests:
                return
            status, response = self.obs.handle_request(request_type, request_data)
            self._push(
                {
                    "op": 7,
                    "d": {
                        "requestType": request_type,
                        "requestId": payload.get("requestId"),
                        "requestStatus": status,
                        "responseData": response,
                    },
                }
            )

    def recv(self) -> str | None:
        try:
            item = self._incoming.get(timeout=0.02)
        except queue.Empty:
            return ""
        return item

    def close(self) -> None:
        self._closed = True
        self._push(None)

    # -- 服务端辅助 ---------------------------------------------------------- #
    def server_close(self) -> None:
        self._closed = True
        self._push(None)

    def push_event(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        self._push({"op": 5, "d": {"eventType": event_type, "eventIntent": 1, "eventData": data or {}}})

    def _push(self, payload: dict[str, Any] | None) -> None:
        self._incoming.put(None if payload is None else json.dumps(payload))


class FakeControl:
    """AudioStateManager / EventManager 需要的 OBS 控制替身。"""

    def __init__(
        self,
        states: dict[str, AudioSourceState] | None = None,
        *,
        connected: bool = True,
    ) -> None:
        self.states = states or {
            "桌面音频": AudioSourceState("桌面音频", muted=False, volume_mul=0.7)
        }
        self._connected = connected
        self.calls: list[tuple[str, Any]] = []
        self.fail_on_set: Exception | None = None

    @property
    def connected(self) -> bool:
        return self._connected

    @connected.setter
    def connected(self, value: bool) -> None:
        self._connected = value

    def require_source(self, name: str) -> None:
        if name not in self.states:
            raise OBSSourceError(f"OBS 中找不到音源「{name}」")

    def get_state(self, name: str) -> AudioSourceState:
        self.require_source(name)
        return self.states[name]

    def set_muted(self, name: str, muted: bool) -> None:
        if self.fail_on_set:
            raise self.fail_on_set
        self.require_source(name)
        current = self.states[name]
        self.states[name] = AudioSourceState(name, bool(muted), current.volume_mul)
        self.calls.append(("set_muted", name, bool(muted)))

    def set_volume_mul(self, name: str, volume_mul: float) -> None:
        if self.fail_on_set:
            raise self.fail_on_set
        self.require_source(name)
        current = self.states[name]
        self.states[name] = AudioSourceState(name, current.muted, float(volume_mul))
        self.calls.append(("set_volume_mul", name, float(volume_mul)))

    # -- 测试辅助 ------------------------------------------------------------ #
    def simulate_user_mute(self, name: str, muted: bool) -> None:
        current = self.states[name]
        self.states[name] = AudioSourceState(name, muted, current.volume_mul)

    def simulate_user_volume(self, name: str, volume: float) -> None:
        current = self.states[name]
        self.states[name] = AudioSourceState(name, current.muted, volume)


class FakePlayer:
    """按「持续时长」自动结束的播放器替身。"""

    def __init__(self, duration: float = 0.15, fail_with: Exception | None = None) -> None:
        self._duration = float(duration)
        self.fail_with = fail_with
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._until = 0.0
        self.stop_count = 0
        self._lock = threading.RLock()

    @property
    def is_active(self) -> bool:
        with self._lock:
            return time.monotonic() < self._until

    @property
    def duration(self) -> float:
        return self._duration

    @duration.setter
    def duration(self, value: float) -> None:
        self._duration = float(value)

    @property
    def duration_s(self) -> float:
        return self._duration

    @property
    def position(self) -> float:
        return 0.0

    @property
    def progress(self) -> float:
        return 0.0

    def load(self, path: str) -> SimpleNamespace:
        return SimpleNamespace(
            path=str(path),
            duration_s=self._duration,
            samplerate=48000,
            channels=2,
            format="WAV",
        )

    def list_output_devices(self) -> list[tuple[int, str]]:
        return []

    def prepare(self, path: str):
        return None

    def is_prepared(self, path: str) -> bool:
        return False

    def close(self) -> None:
        self.stop()

    def play(self, path: str | None = None, **kwargs: Any) -> SimpleNamespace:
        if self.fail_with is not None:
            raise self.fail_with
        with self._lock:
            self.calls.append((str(path), kwargs))
            self._until = time.monotonic() + self._duration
        return SimpleNamespace(duration_s=self._duration)

    def stop(self) -> None:
        with self._lock:
            self._until = 0.0
            self.stop_count += 1


class FakeRecognizer:
    """语音识别替身：返回固定文本或抛错。"""

    name = "fake"

    def __init__(self, text: str = "", error: Exception | None = None) -> None:
        self.text = text
        self.error = error
        self.calls = 0

    @property
    def available(self) -> bool:
        return True

    def warmup(self) -> None:
        return None

    def transcribe(self, pcm, samplerate):
        from rusty_moment.detection.speech_recognizer import Transcript

        self.calls += 1
        if self.error is not None:
            raise self.error
        return Transcript(
            text=self.text,
            language="zh",
            duration_s=len(pcm) / float(samplerate),
            latency_ms=1.0,
        )


class FakeCapture:
    """麦克风替身：按顺序吐出预置音频块，然后返回 None。"""

    def __init__(self, blocks: list) -> None:
        self._blocks = list(blocks)
        self.started = False
        self.stopped = False
        self.start_args: dict = {}
        self.devices = []

    def start(self, device=None, *, samplerate=16000, blocksize=480, channels=1) -> None:
        self.started = True
        self.start_args = {
            "device": device,
            "samplerate": samplerate,
            "blocksize": blocksize,
            "channels": channels,
        }

    def stop(self) -> None:
        self.stopped = True

    def read(self, timeout: float = 0.5):
        if not self._blocks:
            time.sleep(min(0.005, timeout))
            return None
        return self._blocks.pop(0)

    def list_input_devices(self):
        return self.devices


class FakeAnalyzer:
    """AI 语义分析替身：记录每次调用拿到的文本与上下文。"""

    name = "fake"

    def __init__(self, scores=None, error: Exception | None = None) -> None:
        from rusty_moment.detection.emotion_analyzer import EmotionScores

        self.scores = scores or EmotionScores(
            dramatic_score=0.9,
            context_score=0.8,
            comedic_potential=0.4,
            reason="假装很悲壮",
            confidence=0.9,
        )
        self.error = error
        self.calls: list[tuple[str, list[str]]] = []

    @property
    def available(self) -> bool:
        return True

    def analyze(self, text, context=None):
        self.calls.append((text, list(context or [])))
        if self.error is not None:
            raise self.error
        return self.scores

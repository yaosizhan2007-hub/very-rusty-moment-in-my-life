"""OBS WebSocket 5.x 协议的最小实现（不依赖第三方 OBS SDK）。

为什么自己写而不是用现成封装：请求-响应、超时、断线回调、可注入传输层
（测试里用 FakeTransport 模拟 OBS）这几件事是本项目音频状态正确性的关键，
自己掌握几百行代码比穿透一层封装更好调试、也更容易打包。

协议（obs-websocket v5）:
    op 0 Hello              {"obsWebSocketVersion","rpcVersion","authentication"?}
    op 1 Identify           {"rpcVersion":1,"authentication"?,"eventSubscriptions"?}
    op 2 Identified         {"negotiatedRpcVersion"}
    op 5 Event              {"eventType","eventIntent","eventData"}
    op 6 Request            {"requestType","requestId","requestData"?}
    op 7 RequestResponse    {"requestType","requestId","requestStatus","responseData"?}
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Protocol

log = logging.getLogger(__name__)

DEFAULT_RPC_VERSION = 1
EVENT_SUB_GENERAL = 1 << 0


class OBSConnectionError(Exception):
    """无法连接 OBS，或连接在请求过程中断开。"""


class OBSTimeoutError(Exception):
    """等待 OBS 响应超时。"""


class OBSRequestError(Exception):
    """OBS 正常回了响应，但 requestStatus.result 为 false。"""

    def __init__(self, request_type: str, code: int, comment: str) -> None:
        super().__init__(f"{request_type} 失败（code={code}）：{comment}")
        self.request_type = request_type
        self.code = code
        self.comment = comment


class OBSAuthError(OBSConnectionError):
    """密码错误 / 认证失败。"""


class ConnectionState(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"


class Transport(Protocol):
    """OBS WebSocket 传输层，便于替换/测试。"""

    def connect(self, url: str, timeout: float) -> None: ...

    def send(self, text: str) -> None: ...

    def recv(self) -> str | None: ...

    def close(self) -> None: ...


class WebsocketTransport:
    """基于 websocket-client 的默认传输层。"""

    def __init__(self) -> None:
        self._ws: Any = None

    def connect(self, url: str, timeout: float) -> None:
        import websocket  # 延迟导入：单元测试不需要它

        self._ws = websocket.create_connection(url, timeout=timeout, enable_multithread=True)
        # 把读超时设长一些，避免空闲时误判断线；断线由 OBS 侧关闭或异常检测
        self._ws.settimeout(1.0)

    def send(self, text: str) -> None:
        if self._ws is None:
            raise OBSConnectionError("WebSocket 未连接")
        self._ws.send(text)

    def recv(self) -> str | None:
        import websocket

        if self._ws is None:
            return None
        try:
            return self._ws.recv()
        except websocket.WebSocketTimeoutException:
            return ""  # 空串表示「暂时没数据」，调用方继续轮询
        except websocket.WebSocketConnectionClosedException:
            return None
        except OSError:
            return None

    def close(self) -> None:
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                ws.close()
            except Exception:  # pragma: no cover
                pass


def _build_auth_string(password: str, salt: str, challenge: str) -> str:
    secret = base64.b64encode(hashlib.sha256((password + salt).encode("utf-8")).digest())
    auth = base64.b64encode(hashlib.sha256(secret + challenge.encode("utf-8")).digest())
    return auth.decode("ascii")


@dataclass
class _Pending:
    request_type: str
    status: dict[str, Any] | None = None
    response: dict[str, Any] | None = None
    error: Exception | None = None
    event: threading.Event = field(default_factory=threading.Event)


PasswordProvider = Callable[[], str | None]
StateCallback = Callable[[ConnectionState, str], None]
EventCallback = Callable[[str, dict[str, Any]], None]


class OBSClient:
    """线程安全、带超时的 OBS WebSocket 客户端。

    所有公开方法都可以从不同线程调用；接收线程只做解析与派发，不阻塞。
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 4455,
        password_provider: PasswordProvider | None = None,
        *,
        transport_factory: Callable[[], Transport] | None = None,
        timeout: float = 5.0,
        on_state_change: StateCallback | None = None,
        on_event: EventCallback | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self._password_provider = password_provider or (lambda: None)
        self._transport_factory = transport_factory or WebsocketTransport
        self.timeout = timeout
        self.on_state_change = on_state_change
        self.on_event = on_event

        self._lock = threading.RLock()
        self._send_lock = threading.Lock()
        self._transport: Transport | None = None
        self._rx_thread: threading.Thread | None = None
        self._pending: dict[str, _Pending] = {}
        self._request_seq = 0
        self._closing = threading.Event()
        self.state = ConnectionState.DISCONNECTED
        self.last_error = ""
        self.obs_version = ""

    # -- 连接管理 ----------------------------------------------------------- #
    @property
    def connected(self) -> bool:
        return self.state == ConnectionState.CONNECTED

    def set_password_provider(self, provider: PasswordProvider) -> None:
        """在运行时更换密码来源（用户刚在设置里输入了密码，或改了保存的凭据）。"""
        self._password_provider = provider

    @property
    def url(self) -> str:
        return f"ws://{self.host}:{self.port}"

    def connect(self) -> None:
        if self.state == ConnectionState.CONNECTED:
            return
        self._closing.clear()
        self._set_state(ConnectionState.CONNECTING, "")
        transport = self._transport_factory()
        try:
            transport.connect(self.url, self.timeout)
        except Exception as exc:
            self._set_state(ConnectionState.ERROR, f"连接失败：{exc}")
            raise OBSConnectionError(f"无法连接 {self.url}：{exc}") from exc

        self._transport = transport
        try:
            self._handshake(transport)
        except Exception:
            try:
                transport.close()
            finally:
                self._transport = None
            raise

        self._rx_thread = threading.Thread(
            target=self._rx_loop, name="obs-rx", daemon=True
        )
        self._rx_thread.start()
        self._set_state(ConnectionState.CONNECTED, "")

    def _handshake(self, transport: Transport) -> None:
        hello_text = self._read_one(transport)
        hello = json.loads(hello_text)
        if hello.get("op") != 0:
            raise OBSConnectionError("OBS 未返回 Hello 消息，可能不是 obs-websocket 5.x")
        data = hello.get("d", {})
        self.obs_version = str(data.get("obsWebSocketVersion", ""))
        auth_info = data.get("authentication") or {}
        identify: dict[str, Any] = {
            "rpcVersion": DEFAULT_RPC_VERSION,
            "eventSubscriptions": EVENT_SUB_GENERAL,
        }
        if auth_info:
            password = self._password_provider() or ""
            if not password:
                raise OBSAuthError("OBS 要求密码，但当前没有可用密码（请在设置中填写）")
            identify["authentication"] = _build_auth_string(
                password, str(auth_info.get("salt", "")), str(auth_info.get("challenge", ""))
            )
        self._send_json(transport, {"op": 1, "d": identify})

        identified_text = self._read_one(transport)
        identified = json.loads(identified_text)
        if identified.get("op") == 4:
            # RequestBatch/Request 不属于握手阶段，出现即协议异常
            raise OBSConnectionError("握手阶段收到意外消息")
        if identified.get("op") != 2:
            raise OBSAuthError("OBS 认证失败（密码可能不正确）")
        negotiated = identified.get("d", {}).get("negotiatedRpcVersion")
        if negotiated and int(negotiated) != DEFAULT_RPC_VERSION:
            log.warning("OBS 协商 RPC 版本为 %s，客户端按 v1 处理", negotiated)

    def _read_one(self, transport: Transport, timeout: float | None = None) -> str:
        limit = timeout if timeout is not None else self.timeout
        waited = 0.0
        step = 0.05
        while waited < limit:
            text = transport.recv()
            if text is None:
                raise OBSConnectionError("OBS 在握手阶段关闭了连接")
            if text:
                return text
            time.sleep(step)
            waited += step
        raise OBSTimeoutError("等待 OBS 握手消息超时")

    def close(self) -> None:
        self._closing.set()
        transport, self._transport = self._transport, None
        if transport is not None:
            try:
                transport.close()
            except Exception:  # pragma: no cover
                pass
        thread, self._rx_thread = self._rx_thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=3.0)
        self._fail_pending(OBSConnectionError("连接已关闭"))
        self._set_state(ConnectionState.DISCONNECTED, "")

    # -- 请求 --------------------------------------------------------------- #
    def request(
        self,
        request_type: str,
        request_data: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            if self.state != ConnectionState.CONNECTED or self._transport is None:
                raise OBSConnectionError("OBS 未连接")
            self._request_seq += 1
            request_id = f"rm-{self._request_seq}"
            pending = _Pending(request_type=request_type)
            self._pending[request_id] = pending
            payload = {"op": 6, "d": {"requestType": request_type, "requestId": request_id}}
            if request_data:
                payload["d"]["requestData"] = request_data
            try:
                self._send_json(self._transport, payload)
            except Exception as exc:
                self._pending.pop(request_id, None)
                self._handle_disconnect(f"发送失败：{exc}")
                raise OBSConnectionError(f"发送请求失败：{exc}") from exc

        if not pending.event.wait(timeout or self.timeout):
            with self._lock:
                self._pending.pop(request_id, None)
            raise OBSTimeoutError(f"{request_type} 超时（{timeout or self.timeout}s）")
        if pending.error is not None:
            raise pending.error
        status = pending.status or {}
        if not status.get("result", False):
            raise OBSRequestError(
                request_type,
                int(status.get("code", 0) or 0),
                str(status.get("comment", "") or ""),
            )
        return pending.response or {}

    # -- 内部 --------------------------------------------------------------- #
    def _send_json(self, transport: Transport, payload: dict[str, Any]) -> None:
        text = json.dumps(payload)
        with self._send_lock:
            transport.send(text)

    def _rx_loop(self) -> None:
        transport = self._transport
        if transport is None:
            return
        while not self._closing.is_set():
            text = transport.recv()
            if text is None:
                if not self._closing.is_set():
                    self._handle_disconnect("OBS 关闭了连接")
                return
            if not text:
                continue
            try:
                message = json.loads(text)
            except json.JSONDecodeError:
                log.warning("收到无法解析的 OBS 消息")
                continue
            op = message.get("op")
            data = message.get("d", {})
            if op == 7:
                self._resolve_response(data)
            elif op == 5:
                self._dispatch_event(data)

    def _resolve_response(self, data: dict[str, Any]) -> None:
        request_id = str(data.get("requestId", ""))
        with self._lock:
            pending = self._pending.pop(request_id, None)
        if pending is None:
            log.debug("收到未知 requestId 的响应：%s", request_id)
            return
        pending.status = data.get("requestStatus", {})
        pending.response = data.get("responseData", {})
        pending.event.set()

    def _dispatch_event(self, data: dict[str, Any]) -> None:
        event_type = str(data.get("eventType", ""))
        event_data = data.get("eventData", {}) or {}
        if event_type == "Exiting":
            self._handle_disconnect("OBS 正在退出")
            return
        if self.on_event is not None:
            try:
                self.on_event(event_type, event_data)
            except Exception:  # pragma: no cover - 回调不应影响接收线程
                log.exception("OBS 事件回调异常：%s", event_type)

    def _handle_disconnect(self, reason: str) -> None:
        if self._closing.is_set() and self.state == ConnectionState.DISCONNECTED:
            return
        log.warning("OBS 连接断开：%s", reason)
        transport, self._transport = self._transport, None
        if transport is not None:
            try:
                transport.close()
            except Exception:  # pragma: no cover
                pass
        self._fail_pending(OBSConnectionError(f"连接已断开：{reason}"))
        self._set_state(ConnectionState.DISCONNECTED, reason)

    def _fail_pending(self, exc: Exception) -> None:
        with self._lock:
            pending_items = list(self._pending.values())
            self._pending.clear()
        for pending in pending_items:
            pending.error = exc
            pending.event.set()

    def _set_state(self, state: ConnectionState, detail: str) -> None:
        changed = state != self.state or detail != self.last_error
        self.state = state
        self.last_error = detail
        if changed and self.on_state_change is not None:
            try:
                self.on_state_change(state, detail)
            except Exception:  # pragma: no cover
                log.exception("连接状态回调异常")

"""OBS WebSocket 客户端协议测试（握手 / 认证 / 请求响应 / 超时 / 断线）。"""

from __future__ import annotations

import time

import pytest

from fakes import FakeOBS
from rusty_moment.obs.obs_client import (
    ConnectionState,
    OBSAuthError,
    OBSClient,
    OBSConnectionError,
    OBSRequestError,
    OBSTimeoutError,
)


def make_client(obs: FakeOBS, password: str = "s3cret", **kwargs) -> OBSClient:
    return OBSClient(
        host="127.0.0.1",
        port=4455,
        password_provider=lambda: password,
        transport_factory=obs.transport_factory(),
        timeout=kwargs.pop("timeout", 1.0),
        **kwargs,
    )


def wait_for(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_connect_and_request():
    obs = FakeOBS()
    client = make_client(obs)
    states: list[ConnectionState] = []
    client.on_state_change = lambda state, detail: states.append(state)

    client.connect()

    assert client.connected
    assert client.obs_version == "5.5.6"
    data = client.request("GetVersion")
    assert data["obsVersion"] == "32.2.2"
    assert ConnectionState.CONNECTED in states

    client.close()
    assert not client.connected
    assert states[-1] == ConnectionState.DISCONNECTED


def test_connect_without_password_when_server_requires_it():
    obs = FakeOBS()
    client = make_client(obs, password="")
    with pytest.raises(OBSAuthError):
        client.connect()
    assert not client.connected


def test_wrong_password_raises_auth_error():
    obs = FakeOBS()
    client = make_client(obs, password="wrong-password")
    with pytest.raises((OBSAuthError, OBSConnectionError)):
        client.connect()
    assert obs.auth_failures == 1
    assert not client.connected


def test_request_error_propagates_status():
    obs = FakeOBS()
    client = make_client(obs)
    client.connect()
    try:
        with pytest.raises(OBSRequestError) as excinfo:
            client.request("NoSuchRequest")
        assert excinfo.value.code == 204
    finally:
        client.close()


def test_request_timeout():
    obs = FakeOBS()
    obs.drop_requests = True
    client = make_client(obs)
    client.connect()
    try:
        with pytest.raises(OBSTimeoutError):
            client.request("GetVersion", timeout=0.15)
    finally:
        client.close()


def test_server_disconnect_marks_state_and_fails_requests():
    obs = FakeOBS()
    client = make_client(obs)
    client.connect()
    obs.disconnect_all()

    assert wait_for(lambda: not client.connected)
    assert client.state in (ConnectionState.DISCONNECTED, ConnectionState.ERROR)
    with pytest.raises(OBSConnectionError):
        client.request("GetVersion")
    client.close()


def test_exiting_event_triggers_disconnect():
    obs = FakeOBS()
    events: list[str] = []
    client = make_client(obs, on_event=lambda name, data: events.append(name))
    client.connect()
    transport = obs.transports[-1]
    transport.push_event("Exiting", {"code": 0})

    assert wait_for(lambda: not client.connected)
    assert "Exiting" in events or not client.connected
    client.close()


def test_request_before_connect_raises():
    obs = FakeOBS()
    client = make_client(obs)
    with pytest.raises(OBSConnectionError):
        client.request("GetVersion")


def test_connect_twice_is_idempotent():
    obs = FakeOBS()
    client = make_client(obs)
    client.connect()
    client.connect()
    assert obs.identify_count == 1
    client.close()

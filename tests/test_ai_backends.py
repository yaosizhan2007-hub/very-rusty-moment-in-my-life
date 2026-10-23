"""AI 后端测试：请求构造、结构化校验、错误映射、配置工厂。

用 httpx.MockTransport 模拟服务端，不产生真实网络请求。
"""

from __future__ import annotations

import json

import httpx
import pytest

from rusty_moment.config.config_manager import AIConfig
from rusty_moment.detection.ai_backends import (
    DEFAULT_SYSTEM_PROMPT,
    LocalLLMAnalyzer,
    OpenAICompatibleAnalyzer,
    build_analyzer,
    chat_completions_url,
    extract_json_object,
)
from rusty_moment.detection.emotion_analyzer import AnalysisError, EmotionScores, NullAnalyzer

VALID = {
    "dramatic_score": 0.92,
    "context_score": 0.8,
    "comedic_potential": 0.7,
    "confidence": 0.85,
    "reason": "像遗言",
}


def make_analyzer(handler, **kwargs) -> OpenAICompatibleAnalyzer:
    client = httpx.Client(transport=httpx.MockTransport(handler), timeout=2.0)
    options = dict(base_url="http://fake.local/v1", model="test-model", api_key="k-test")
    options.update(kwargs)
    return OpenAICompatibleAnalyzer(http_client=client, **options)


def ok_handler(content: str, captured: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured.append(request)
        return httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": content}}]}
        )

    return handler


def test_chat_url_normalization():
    assert chat_completions_url("http://host:11434") == "http://host:11434/v1/chat/completions"
    assert chat_completions_url("http://host:11434/v1") == "http://host:11434/v1/chat/completions"
    assert (
        chat_completions_url("https://api.x.com/v1/chat/completions")
        == "https://api.x.com/v1/chat/completions"
    )
    assert chat_completions_url("") == ""


def test_extract_json_plain_and_fenced():
    assert extract_json_object('{"a": 1}') == {"a": 1}
    assert extract_json_object('```json\n{"a": 2}\n```') == {"a": 2}
    assert extract_json_object('好的：{"a": 3} 以上') == {"a": 3}
    with pytest.raises(AnalysisError):
        extract_json_object("")
    with pytest.raises(AnalysisError):
        extract_json_object("完全不是 JSON")
    with pytest.raises(AnalysisError):
        extract_json_object("[1, 2, 3]")


def test_successful_analysis_returns_scores():
    analyzer = make_analyzer(ok_handler(json.dumps(VALID)))
    scores = analyzer.analyze("兄弟，别管我")
    assert isinstance(scores, EmotionScores)
    assert scores.dramatic_score == pytest.approx(0.92)
    assert scores.reason == "像遗言"
    assert 0.0 <= scores.score <= 1.0


def test_request_payload_and_headers():
    captured: list[httpx.Request] = []
    analyzer = make_analyzer(ok_handler(json.dumps(VALID), captured))
    analyzer.analyze("别管我", ["今天吃啥", "我买了奶茶"])

    assert len(captured) == 1
    request = captured[0]
    assert request.url.path == "/v1/chat/completions"
    assert request.headers["Authorization"] == "Bearer k-test"
    payload = json.loads(request.content)
    assert payload["model"] == "test-model"
    assert "response_format" not in payload, "默认不发 response_format（Ollama 兼容层有坑）"
    assert payload["messages"][0]["role"] == "system"
    user_content = payload["messages"][1]["content"]
    assert "别管我" in user_content
    assert "今天吃啥" in user_content, "最近几句上下文应进 prompt"
    assert payload["messages"][0]["content"] == DEFAULT_SYSTEM_PROMPT


def test_json_mode_is_opt_in():
    captured: list[httpx.Request] = []
    analyzer = make_analyzer(ok_handler(json.dumps(VALID), captured), use_json_mode=True)
    analyzer.analyze("别管我")
    payload = json.loads(captured[0].content)
    assert payload["response_format"] == {"type": "json_object"}


def test_custom_system_prompt_is_used():
    captured: list[httpx.Request] = []
    analyzer = make_analyzer(
        ok_handler(json.dumps(VALID), captured), system_prompt="自定义提示词"
    )
    analyzer.analyze("别管我")
    payload = json.loads(captured[0].content)
    assert payload["messages"][0]["content"] == "自定义提示词"


def test_auth_failure_maps_to_error():
    analyzer = make_analyzer(lambda request: httpx.Response(401, json={"error": "bad key"}))
    with pytest.raises(AnalysisError) as excinfo:
        analyzer.analyze("别管我")
    assert "认证失败" in str(excinfo.value)


def test_not_found_maps_to_error():
    analyzer = make_analyzer(lambda request: httpx.Response(404, json={}))
    with pytest.raises(AnalysisError) as excinfo:
        analyzer.analyze("别管我")
    assert "接口不存在" in str(excinfo.value)


def test_server_error_maps_to_error():
    analyzer = make_analyzer(lambda request: httpx.Response(500, text="boom"))
    with pytest.raises(AnalysisError) as excinfo:
        analyzer.analyze("别管我")
    assert "500" in str(excinfo.value)


def test_timeout_maps_to_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out")

    analyzer = make_analyzer(handler)
    with pytest.raises(AnalysisError) as excinfo:
        analyzer.analyze("别管我")
    assert "超时" in str(excinfo.value)


def test_connection_error_maps_to_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    analyzer = make_analyzer(handler)
    with pytest.raises(AnalysisError) as excinfo:
        analyzer.analyze("别管我")
    assert "请求失败" in str(excinfo.value)


def test_invalid_json_content_is_rejected():
    analyzer = make_analyzer(ok_handler("我今天不想输出 JSON"))
    with pytest.raises(AnalysisError):
        analyzer.analyze("别管我")


def test_missing_fields_is_rejected():
    analyzer = make_analyzer(ok_handler(json.dumps({"dramatic_score": 0.9})))
    with pytest.raises(AnalysisError):
        analyzer.analyze("别管我")


def test_malformed_response_body_is_rejected():
    analyzer = make_analyzer(lambda request: httpx.Response(200, json={"nope": True}))
    with pytest.raises(AnalysisError) as excinfo:
        analyzer.analyze("别管我")
    assert "结构异常" in str(excinfo.value)


def test_available_requires_key_and_model():
    analyzer = OpenAICompatibleAnalyzer(base_url="http://x/v1", model="", api_key="k")
    assert not analyzer.available
    assert "模型" in analyzer.unavailable_reason

    analyzer = OpenAICompatibleAnalyzer(base_url="http://x/v1", model="m", api_key="")
    assert not analyzer.available
    assert "API Key" in analyzer.unavailable_reason

    analyzer = OpenAICompatibleAnalyzer(base_url="", model="m", api_key="k")
    assert not analyzer.available


def test_local_backend_needs_no_key():
    analyzer = LocalLLMAnalyzer(model="qwen3:8b")
    assert analyzer.available
    assert analyzer.base_url.startswith("http://127.0.0.1:11434")
    assert analyzer.name == "local_llm"


def test_content_falls_back_to_reasoning_field():
    """qwen3 在 token 给少时只输出思考，content 为空——真实遇到过的坑。"""
    payload = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "reasoning": '思考中……最终：{"dramatic_score":0.1,"context_score":0.2,'
                    '"comedic_potential":0.8,"confidence":0.6,"reason":"闲聊"}',
                }
            }
        ]
    }
    analyzer = make_analyzer(lambda request: httpx.Response(200, json=payload))
    scores = analyzer.analyze("今天吃啥")
    assert scores.dramatic_score == pytest.approx(0.1)
    assert scores.reason == "闲聊"


def test_empty_content_without_reasoning_raises():
    payload = {"choices": [{"message": {"role": "assistant", "content": ""}}]}
    analyzer = make_analyzer(lambda request: httpx.Response(200, json=payload))
    with pytest.raises(AnalysisError) as excinfo:
        analyzer.analyze("别管我")
    assert "空内容" in str(excinfo.value)


def test_default_max_tokens_leaves_room_for_thinking():
    captured: list[httpx.Request] = []
    analyzer = make_analyzer(ok_handler(json.dumps(VALID), captured))
    analyzer.analyze("别管我")
    payload = json.loads(captured[0].content)
    assert payload["max_tokens"] >= 512, "思考型模型需要更大的输出预算，否则 content 会为空"


def test_build_analyzer_respects_config():
    assert isinstance(build_analyzer(AIConfig(enabled=False)), NullAnalyzer)
    local = build_analyzer(
        AIConfig(enabled=True, backend="local_llm", model="qwen3:8b"), lambda: ""
    )
    assert isinstance(local, LocalLLMAnalyzer)


# --------------------------------------------------------------------------- #
# temperature 兼容性：Claude 5 系等新模型直接拒绝该参数
# --------------------------------------------------------------------------- #
def test_temperature_rejected_retries_without_it():
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        payloads.append(body)
        if "temperature" in body:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "code": "invalid_request_error",
                        "message": "`temperature` is deprecated for this model.",
                        "type": "invalid_request",
                    }
                },
            )
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(VALID)}}]})

    analyzer = make_analyzer(handler)
    scores = analyzer.analyze("兄弟，别管我，你们快走")

    assert scores.dramatic_score == pytest.approx(0.92)
    assert len(payloads) == 2, "应当只重试一次"
    assert "temperature" in payloads[0]
    assert "temperature" not in payloads[1]

    # 记住这次拒绝，后续请求不再浪费一个 400
    analyzer.analyze("第二句")
    assert "temperature" not in payloads[2]


def test_temperature_kept_when_service_accepts_it():
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(VALID)}}]})

    make_analyzer(handler).analyze("别管我")
    assert payloads[0]["temperature"] == 0.0


def test_temperature_none_is_never_sent():
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(VALID)}}]})

    make_analyzer(handler, temperature=None).analyze("别管我")
    assert "temperature" not in payloads[0]


def test_unrelated_400_is_not_retried():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(400, json={"error": {"message": "model not found"}})

    with pytest.raises(AnalysisError) as excinfo:
        make_analyzer(handler).analyze("别管我")
    assert len(calls) == 1, "与 temperature 无关的 400 不该重试"
    assert "400" in str(excinfo.value)

    cloud = build_analyzer(
        AIConfig(enabled=True, backend="openai_compatible", model="gpt-4o-mini"),
        lambda: "secret-key",
    )
    assert isinstance(cloud, OpenAICompatibleAnalyzer)
    assert cloud.available

    cloud_no_key = build_analyzer(
        AIConfig(enabled=True, backend="openai_compatible", model="gpt-4o-mini"), lambda: ""
    )
    assert cloud_no_key.available is False

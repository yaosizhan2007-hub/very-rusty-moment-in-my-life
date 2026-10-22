"""AI 语义分析后端：OpenAI 兼容接口 + 本地 LLM（Ollama 兼容）。

设计约束（对应提示词 4.4）：
- 只发送**文本**（识别结果 + 最近几句上下文），绝不发音频；
- 必须能在超时/报错/返回非法数据时干净地抛 :class:`AnalysisError`，
  由 TriggerEngine 重新归一化权重回退到关键词+规则；
- 只输出结构化分数，**不碰音频设备**；
- 云端后端必须由用户显式同意（这一层在 DetectionPipeline 里检查 cloud_consent，
  这里只负责「拿不到 key 就不装作能用」）。

HTTP 层用 httpx，可注入 ``http_client`` 便于单测（MockTransport）。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable

from ..config.config_manager import AIConfig
from .emotion_analyzer import (
    AnalysisError,
    AnalysisUnavailable,
    EmotionScores,
    NullAnalyzer,
    parse_scores,
)

log = logging.getLogger(__name__)

DEFAULT_SYSTEM_PROMPT = (
    "你是一个游戏直播场景的语义分析器。判断说话者是否刚刚说出了一句「悲壮的、"
    "像是要牺牲/认输/告别」的话。\n"
    "只输出 JSON 对象，不要输出任何解释、Markdown 或代码块，字段固定为：\n"
    '{"dramatic_score": 0~1, "context_score": 0~1, "comedic_potential": 0~1, '
    '"confidence": 0~1, "reason": "不超过20个汉字"}\n'
    "dramatic_score 表示这句台词的悲壮程度；context_score 表示它在当前对局语境里是否说得通；"
    "comedic_potential 表示如果此刻强行插入煽情 BGM 的反差喜剧效果；"
    "confidence 是你对自己判断的把握。普通聊天、点外卖、报点、骂人一律给低分。\n"
    "注意：所有分数都是 0~1 的小数（例如 0.15），字段名必须使用上面的英文键名；"
    "不要输出思考过程、不要解释、不要用代码块包裹。"
)

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def chat_completions_url(base_url: str) -> str:
    """容忍用户填 `http://host:port`、`.../v1` 或完整 URL。"""
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return ""
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


def extract_json_object(content: str) -> dict[str, Any]:
    """从模型输出里抠出第一个 JSON 对象（模型偶尔会包 ```json 或加解释）。"""
    text = (content or "").strip()
    if not text:
        raise AnalysisError("AI 返回了空内容")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = _JSON_BLOCK.search(text)
        if not match:
            raise AnalysisError(f"AI 返回的不是 JSON：{text[:80]}")
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise AnalysisError(f"AI 返回的 JSON 无法解析：{exc}") from exc
    if not isinstance(parsed, dict):
        raise AnalysisError("AI 返回的 JSON 不是对象")
    return parsed


class OpenAICompatibleAnalyzer:
    """任何提供 ``/v1/chat/completions`` 的服务都能用（OpenAI、DeepSeek、Ollama…）。"""

    name = "openai_compatible"

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str = "",
        timeout: float = 4.0,
        max_tokens: int = 1200,
        temperature: float = 0.0,
        system_prompt: str = "",
        http_client: Any = None,
        require_api_key: bool = True,
        use_json_mode: bool = False,
    ) -> None:
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        self.timeout = max(0.5, float(timeout))
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.system_prompt = (system_prompt or DEFAULT_SYSTEM_PROMPT).strip()
        self._client = http_client
        self._require_api_key = require_api_key
        # Ollama 的 OpenAI 兼容层对 response_format=json_object 支持不完整：
        # 实测 qwen3 在带这个字段时会把思考内容写进 content，导致解析失败。
        # 所以默认不发，靠 system prompt 约束 + 本地 JSON 提取兜底。
        self.use_json_mode = use_json_mode

    # -- 能力 --------------------------------------------------------------- #
    @property
    def available(self) -> bool:
        if not self.base_url or not self.model:
            return False
        if self._require_api_key and not self.api_key:
            return False
        return True

    @property
    def unavailable_reason(self) -> str:
        if not self.base_url:
            return "未配置 AI 服务地址"
        if not self.model:
            return "未配置模型名称"
        if self._require_api_key and not self.api_key:
            return "未配置 API Key"
        return ""

    def _http(self) -> Any:
        if self._client is None:
            import httpx

            self._client = httpx.Client(timeout=self.timeout)
            self._owns_client = True
        return self._client

    # -- 分析 --------------------------------------------------------------- #
    def analyze(self, text: str, context: list[str] | None = None) -> EmotionScores:
        if not self.available:
            raise AnalysisUnavailable(self.unavailable_reason or "AI 未配置")
        payload = self._build_payload(text, context or [])
        url = chat_completions_url(self.base_url)
        if not url:  # pragma: no cover - available 已挡住
            raise AnalysisUnavailable("AI 服务地址无效")

        import httpx

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            response = self._http().post(url, json=payload, headers=headers, timeout=self.timeout)
        except httpx.TimeoutException as exc:
            raise AnalysisError(f"AI 请求超时（{self.timeout:.1f}s）") from exc
        except Exception as exc:  # 连接失败、DNS、代理…
            raise AnalysisError(f"AI 请求失败：{exc}") from exc

        status = int(getattr(response, "status_code", 0) or 0)
        if status in (401, 403):
            raise AnalysisError("AI 认证失败（API Key 无效或无权限）")
        if status == 404:
            raise AnalysisError(f"AI 接口不存在：{url}（检查地址与 /v1）")
        if status >= 400:
            excerpt = str(getattr(response, "text", ""))[:120]
            raise AnalysisError(f"AI 服务返回 {status}：{excerpt}")

        try:
            body = response.json()
        except Exception as exc:
            raise AnalysisError(f"AI 响应不是 JSON：{exc}") from exc

        content = self._extract_content(body)
        scores = parse_scores(extract_json_object(content))
        log.info("AI 分析完成：%s", scores.describe())
        return scores

    # -- 内部 --------------------------------------------------------------- #
    def _build_payload(self, text: str, context: list[str]) -> dict[str, Any]:
        user_parts = []
        if context:
            recent = "\n".join(f"- {item}" for item in context[-5:] if item)
            if recent:
                user_parts.append(f"最近几句（按时间顺序）：\n{recent}")
        user_parts.append(f"当前这句：{text}")
        user_parts.append('请只输出一个 JSON 对象，例如 {"dramatic_score":0.1,"context_score":0.1,"comedic_potential":0.9,"confidence":0.8,"reason":"普通闲聊"}')
        payload: dict[str, Any] = {
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": "\n\n".join(user_parts)},
            ],
        }
        if self.use_json_mode:
            payload["response_format"] = {"type": "json_object"}
        return payload

    def _extract_content(self, body: dict[str, Any]) -> str:
        try:
            choices = body["choices"]
            message = choices[0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise AnalysisError(f"AI 响应结构异常：{str(body)[:120]}") from exc
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):  # 某些服务返回分段内容
            content = "".join(
                str(part.get("text", "")) if isinstance(part, dict) else str(part)
                for part in content
            )
        text = str(content or "")
        if text.strip():
            return text

        # 兜底：qwen3 这类「思考型」模型在 token 给少了时会把内容全塞进 reasoning，
        # content 为空。此时从思考文本里找 JSON，总比直接失败好。
        if isinstance(message, dict):
            for key in ("reasoning_content", "reasoning", "thinking"):
                fallback = message.get(key)
                if isinstance(fallback, str) and fallback.strip():
                    log.debug("AI 的 content 为空，改用 %s 字段（%d 字）", key, len(fallback))
                    return fallback
        raise AnalysisError("AI 返回了空内容（可能是 max_tokens 太小，或模型只输出了思考）")

    def close(self) -> None:
        client, self._client = self._client, None
        if client is not None and getattr(self, "_owns_client", False):
            try:
                client.close()
            except Exception:  # pragma: no cover
                pass


class LocalLLMAnalyzer(OpenAICompatibleAnalyzer):
    """本地 Ollama（自带 OpenAI 兼容层），不需要真的 API Key。"""

    name = "local_llm"

    def __init__(
        self,
        *,
        base_url: str = "http://127.0.0.1:11434/v1",
        model: str = "",
        api_key: str = "ollama",
        timeout: float = 8.0,
        http_client: Any = None,
        system_prompt: str = "",
    ) -> None:
        super().__init__(
            base_url=base_url or "http://127.0.0.1:11434/v1",
            model=model,
            api_key=api_key or "ollama",
            timeout=timeout,
            http_client=http_client,
            system_prompt=system_prompt,
            require_api_key=False,
            max_tokens=1200,
        )


def build_analyzer(
    config: AIConfig,
    api_key_getter: Callable[[], str | None] | None = None,
    http_client: Any = None,
) -> Any:
    """按配置构造分析器；未启用时返回 :class:`NullAnalyzer`。"""
    if not getattr(config, "enabled", False):
        return NullAnalyzer()
    backend = getattr(config, "backend", "none")
    api_key = (api_key_getter() or "") if api_key_getter else ""
    if backend == "openai_compatible":
        return OpenAICompatibleAnalyzer(
            base_url=config.base_url or "https://api.openai.com/v1",
            model=config.model,
            api_key=api_key,
            timeout=config.timeout_seconds,
            system_prompt=getattr(config, "system_prompt", ""),
            http_client=http_client,
        )
    if backend == "local_llm":
        return LocalLLMAnalyzer(
            base_url=config.base_url or "http://127.0.0.1:11434/v1",
            model=config.model,
            # 本地 8B 模型 CPU 推理一般 5~10 秒/句，至少给 5 秒，避免必然超时
            timeout=max(5.0, config.timeout_seconds),
            system_prompt=getattr(config, "system_prompt", ""),
            http_client=http_client,
        )
    return NullAnalyzer()

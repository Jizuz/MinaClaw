"""
统一 OpenAI 兼容客户端
- 通过 settings.openai_base_url 指向 GLM / OpenAI / DeepSeek / vLLM / Ollama
- 支持 stream=True 下 content 增量 + tool_calls 增量
- 通过 on_finish 回调返回 usage（如果有）以便上层计量
"""
from typing import AsyncIterator, List, Dict, Any, Callable, Optional
from openai import AsyncOpenAI

from config import settings
from utils.logger import get_logger, log_extra

log = get_logger("llm")

client = AsyncOpenAI(
    api_key=settings.openai_api_key,
    base_url=settings.openai_base_url,
)


async def chat_stream(
    messages: List[Dict[str, Any]],
    tools: List[Dict] | None = None,
    tool_choice: str = "auto",
    temperature: float = 0.6,
    on_usage: Optional[Callable[[Dict[str, int]], None]] = None,
) -> AsyncIterator[Dict[str, Any]]:
    """
    事件类型：
      {"type": "content",   "delta": "..."}
      {"type": "tool_call", "index": 0, "id": "...",
                            "name": "...", "arguments_delta": "..."}
      {"type": "usage",     "prompt": N, "completion": M, "total": T}
      {"type": "finish",    "reason": "stop|tool_calls|length"}
    """
    kwargs: Dict[str, Any] = {
        "model": settings.openai_model,
        "messages": messages,
        "stream": True,
        "temperature": temperature,
        "stream_options": {"include_usage": True},  # 请求返回 usage
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = tool_choice

    log.debug("llm request",
              extra=log_extra(model=settings.openai_model,
                              msg_count=len(messages),
                              tools=len(tools or [])))

    stream = await client.chat.completions.create(**kwargs)

    async for chunk in stream:
        # 部分兼容端点把 usage 放在最后一个无 choices 的 chunk 中
        usage_obj = getattr(chunk, "usage", None)
        if usage_obj and getattr(usage_obj, "total_tokens", None):
            payload = {
                "prompt": getattr(usage_obj, "prompt_tokens", 0) or 0,
                "completion": getattr(usage_obj, "completion_tokens", 0) or 0,
                "total": getattr(usage_obj, "total_tokens", 0) or 0,
            }
            if on_usage:
                try:
                    on_usage(payload)
                except Exception as e:
                    log.warning(f"on_usage callback failed: {e}")
            yield {"type": "usage", **payload}

        if not chunk.choices:
            continue
        choice = chunk.choices[0]
        delta = choice.delta

        if getattr(delta, "content", None):
            yield {"type": "content", "delta": delta.content}

        for tc in (getattr(delta, "tool_calls", None) or []):
            fn = getattr(tc, "function", None)
            yield {
                "type": "tool_call",
                "index": tc.index,
                "id": tc.id or "",
                "name": (fn.name if fn and fn.name else ""),
                "arguments_delta": (fn.arguments if fn and fn.arguments else ""),
            }

        if choice.finish_reason:
            yield {"type": "finish", "reason": choice.finish_reason}
            return
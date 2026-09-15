"""
记忆压缩器（MemoryCompactor）

- 上下文 token 超过阈值时，将较早的历史消息压缩为一段摘要
- 模型调用统一走 core.openai_client 的 client（非流式，一次取全文）
- token 判定使用 tiktoken 精确计数（utils.token_counter）
- LLM 调用失败时降级为截断式摘要，保证压缩始终可完成
"""
import json
from typing import Any, Dict, List, Optional

from config import settings
from core.openai_client import client
from utils.logger import get_logger, log_extra
from utils.token_counter import (
    count_messages_tokens,
    count_tokens,
    token_meter,
)

log = get_logger("memory_compactor")

SUMMARY_PREFIX = "历史摘要："
_MSG_PREVIEW_LIMIT = 1000  # 单条消息进入摘要 prompt 的最大字符数

_SUMMARY_SYSTEM_PROMPT = """你是对话记忆压缩器。请把下面的历史对话浓缩成一段简明摘要，供后续对话作为上下文参考。

要求：
1. 保留关键事实、结论、用户偏好与未完成事项
2. 保留重要工具调用的意图与结果要点
3. 若提供了已有摘要，在已有摘要的基础上融合更新
4. 直接输出摘要正文，不要任何额外说明，使用中文"""

class MemoryCompactor:
    def __init__(
        self,
        model: Optional[str] = None,
        temperature: float = 0.2,
        keep_recent: int = 6,
        max_summary_tokens: int = 500,
    ):
        self.model = model or settings.openai_model
        self.temperature = temperature
        self.keep_recent = keep_recent
        self.max_summary_tokens = max_summary_tokens

    # ---------- 对外入口 ----------

    async def compress_if_overflow(
        self,
        messages: List[Dict[str, Any]],
        *,
        api_key: str = "anonymous",
        session_id: str = "",
        threshold: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        超过阈值时压缩为：[system] + [历史摘要] + 最近 keep_recent 条消息。
        未超阈值或无法安全切分时原样返回。
        """
        threshold = threshold or settings.max_context_tokens
        before = count_messages_tokens(messages)
        if before <= threshold:
            return messages

        head, old_summary, body, keep = self._split(messages)
        if not body:
            # 全部消息都需保留（如单条巨消息），不做压缩
            return messages

        summary = await self._summarize(
            body, old_summary, api_key=api_key, session_id=session_id)

        compacted = list(head)
        compacted.append({"role": "system",
                          "content": f"{SUMMARY_PREFIX}{summary}"})
        compacted.extend(keep)

        after = count_messages_tokens(compacted)
        log.info("memory compacted",
                 extra=log_extra(before=before, after=after,
                                 kept=len(keep), model=self.model))
        return compacted

    # ---------- 切分 ----------

    def _split(
        self, messages: List[Dict[str, Any]]
    ) -> tuple:
        """
        切分为 (头部system, 旧摘要文本, 待压缩区, 保留区)。
        - 保留首条 system 提示词，不参与压缩
        - 吸收已有的「历史摘要」消息，做增量融合
        - 保留区起点避开 tool 消息，避免破坏 tool_calls 配对
        """
        head: List[Dict[str, Any]] = []
        body_start = 0

        if messages and messages[0].get("role") == "system":
            head = [messages[0]]
            body_start = 1

        old_summary = ""
        if body_start < len(messages):
            first = messages[body_start]
            content = first.get("content") or ""
            if (first.get("role") == "system"
                    and isinstance(content, str)
                    and content.startswith(SUMMARY_PREFIX)):
                old_summary = content[len(SUMMARY_PREFIX):]
                body_start += 1

        body_end = max(body_start, len(messages) - self.keep_recent)
        while body_end < len(messages) and messages[body_end].get("role") == "tool":
            body_end += 1

        return (head, old_summary,
                messages[body_start:body_end], messages[body_end:])


    # ---------- 摘要生成 ----------

    async def _summarize(
        self,
        body: List[Dict[str, Any]],
        old_summary: str,
        *,
        api_key: str,
        session_id: str,
    ) -> str:
        prompt_messages = [
            {"role": "system", "content": _SUMMARY_SYSTEM_PROMPT},
            {"role": "user", "content": self._render(body, old_summary)},
        ]
        est_prompt = count_messages_tokens(prompt_messages)

        text = ""
        prompt_tokens = est_prompt
        completion_tokens = 0
        try:
            resp = await client.chat.completions.create(
                model=self.model,
                messages=prompt_messages,
                temperature=self.temperature,
                max_tokens=self.max_summary_tokens,
                stream=False,
                timeout=60,
            )
            text = (resp.choices[0].message.content or "").strip()
            usage = getattr(resp, "usage", None)
            prompt_tokens = (getattr(usage, "prompt_tokens", None)
                             or est_prompt)
            completion_tokens = (getattr(usage, "completion_tokens", None)
                                 or count_tokens(text))
        except Exception as e:
            log.warning(f"summarize via llm failed, "
                        f"fallback to truncate summary: {e}")
            prompt_tokens = est_prompt
            completion_tokens = 0
        finally:
            self._record_usage(api_key, session_id,
                               prompt_tokens, completion_tokens)

        if not text:
            text = self._fallback_summary(body, old_summary)
        return text

    # ---------- 辅助 ----------

    @staticmethod
    def _to_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        try:
            return json.dumps(content, ensure_ascii=False)
        except Exception:
            return str(content)

    def _render(self, body: List[Dict[str, Any]], old_summary: str) -> str:
        """把历史消息渲染为结构化文本，供摘要 prompt 使用"""
        lines: List[str] = []
        if old_summary:
            lines.append(f"[已有摘要]\n{old_summary}")
        lines.append("[对话历史]")
        for m in body:
            role = m.get("role", "user")
            parts: List[str] = []
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function", {})
                args = (fn.get("arguments") or "")[:200]
                parts.append(f"[调用工具 {fn.get('name', '')} 参数 {args}]")
            text = self._to_text(m.get("content")).strip()
            if text:
                parts.append(text[:_MSG_PREVIEW_LIMIT])
            lines.append(f"{role}: {' '.join(parts) if parts else '(空)'}")
        return "\n".join(lines)

    def _fallback_summary(self, body: List[Dict[str, Any]],
                          old_summary: str) -> str:
        """LLM 不可用时的截断式摘要（尾部优先，最近的消息更重要）"""
        chunks: List[str] = []
        if old_summary:
            chunks.append(old_summary)
        for m in body:
            text = self._to_text(m.get("content")).replace("\n", " ")
            chunks.append(f"{m.get('role', 'user')}: {text[:80]}")
        summary = " | ".join(c for c in chunks if c)
        limit = self.max_summary_tokens * 2  # 粗略的字符上限
        return summary[-limit:] if len(summary) > limit else summary

    def _record_usage(self, api_key: str, session_id: str,
                      prompt: int, completion: int) -> None:
        try:
            token_meter.record(
                api_key=api_key,
                session_id=session_id,
                prompt=prompt,
                completion=completion,
                model=self.model,
                kind="compact",
            )
        except Exception:
            log.exception("record compact usage failed")


memory_compactor = MemoryCompactor()
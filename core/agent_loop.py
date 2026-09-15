"""
ReAct Agent（OpenAI Function Calling 协议）

- 从 registry 获取 tool schemas
- 通过 chat_stream 流式推理
- tool_calls → 执行 Skill → 以 role=tool 回传 → 继续
- 每次调用计算 token 并写入 token_meter
"""
import json
from typing import AsyncIterator, Dict, Any, List

from config import settings
from core.openai_client import chat_stream
from skills.skill_loader import registry
from utils.token_counter import count_messages_tokens, count_tokens, token_meter
from utils.logger import get_logger, get_audit_logger, log_extra

log = get_logger("agent")
audit = get_audit_logger()

SYSTEM_PROMPT = """你是 MinaClaw，一个具备工具调用能力的智能助手。

工作方式：
1. 分析用户请求，判断是否需要调用工具
2. 如需调用工具，按 OpenAI function call 规范输出 tool_calls
3. 观察工具返回结果，再决定下一步
4. 信息足够后，输出最终答案

回答使用中文，结构清晰、内容实用。
"""

class ClawAgent:
    def __init__(self):
        self.tool_schemas: List[dict] = registry.tool_schemas()

    def refresh_tools(self):
        self.tool_schemas = registry.tool_schemas()

    async def run(
        self,
        messages: List[Dict[str, Any]],
        api_key: str,
        session_id: str,
        max_iterations: int | None = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        max_iterations = max_iterations or settings.max_iterations
        tools = self.tool_schemas or None

        for iteration in range(max_iterations):
            assistant_text = ""
            pending: Dict[int, Dict[str, Any]] = {}
            usage_from_api: Dict[str, int] = {}

            # 预估 prompt tokens
            est_prompt = count_messages_tokens(messages)

            def on_usage(u: Dict[str, int]):
                usage_from_api.update(u)

            try:
                async for ev in chat_stream(messages, tools=tools, on_usage=on_usage):
                    if ev["type"] == "content":
                        assistant_text += ev["delta"]
                        yield {"type": "thinking", "delta": ev["delta"]}

                    elif ev["type"] == "tool_call":
                        idx = ev["index"]
                        slot = pending.setdefault(idx, {
                            "id": "", "type": "function",
                            "function": {"name": "", "arguments": ""},
                        })
                        if ev["id"]:
                            slot["id"] = ev["id"]
                        if ev["name"]:
                            slot["function"]["name"] += ev["name"]
                        if ev["arguments_delta"]:
                            slot["function"]["arguments"] += ev["arguments_delta"]

                    elif ev["type"] == "usage":
                        usage_from_api.update(ev)

                    elif ev["type"] == "finish":
                        break

            except Exception as e:
                log.exception("agent llm error")
                yield {"type": "error", "message": str(e)}
                return

            # ---- Token 计量 ----
            if usage_from_api.get("total"):
                prompt = usage_from_api.get("prompt", 0)
                completion = usage_from_api.get("completion", 0)
            else:
                # 兜底估算
                prompt = est_prompt
                completion = count_tokens(assistant_text) + sum(
                    count_tokens(tc["function"]["arguments"])
                    for tc in pending.values()
                )
            used, over = token_meter.record(
                api_key=api_key,
                session_id=session_id,
                prompt=prompt,
                completion=completion,
                model=settings.openai_model,
                kind="chat",
            )
            yield {
                "type": "usage",
                "prompt": prompt,
                "completion": completion,
                "total": prompt + completion,
                "iteration": iteration,
            }
            if over:
                yield {"type": "error", "message": "已达到每日 Token 配额，请明日再试"}

            # ---- 无工具调用 → 结束 ----
            if not pending:
                messages.append({"role": "assistant", "content": assistant_text})
                yield {"type": "done"}
                return

            # ---- 归档 assistant 消息 ----
            tool_calls = [pending[i] for i in sorted(pending.keys())]
            messages.append({
                "role": "assistant",
                "content": assistant_text or None,
                "tool_calls": tool_calls,
            })

            # ---- 执行工具 ----
            for tc in tool_calls:
                fn_name = tc["function"]["name"]
                raw_args = tc["function"]["arguments"] or "{}"
                try:
                    args = json.loads(raw_args)
                except json.JSONDecodeError:
                    args = {}

                yield {
                    "type": "tool_start",
                    "tool": fn_name,
                    "tool_id": tc["id"],
                    "arguments": args,
                }

                # 审计
                audit.info("tool_call", extra=log_extra(tool=fn_name, args=args))

                skill = registry.get(fn_name)
                if not skill:
                    result = {"success": False, "error": f"未知工具: {fn_name}", "output": ""}
                else:
                    try:
                        result = await skill.run(args, {})
                    except Exception as e:
                        log.exception("tool run error", extra=log_extra(tool=fn_name))
                        result = {"success": False, "error": str(e), "output": ""}

                yield {
                    "type": "tool_end",
                    "tool": fn_name,
                    "tool_id": tc["id"],
                    "result": result,
                }

                audit.info("tool_result", extra=log_extra(tool=fn_name, success=result.get("success")))

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": json.dumps(result, ensure_ascii=False),
                })

        yield {"type": "done", "reason": "max_iterations"}


agent = ClawAgent()
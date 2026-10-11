"""
ReAct Agent（OpenAI Function Calling 协议）

- 从 registry 获取 tool schemas
- 通过 chat_stream 流式推理
- tool_calls → 执行 Skill → 以 role=tool 回传 → 继续
- 每次调用计算 token 并写入 token_meter
"""
import asyncio
import json
from typing import AsyncIterator, Dict, Any, List

import httpx

from config import settings
from core.openai_client import chat_stream
from skills.skill_loader import registry
from utils.token_counter import count_messages_tokens, count_tokens, token_meter
from utils.logger import get_logger, get_audit_logger, log_extra

log = get_logger("agent")
audit = get_audit_logger()

# ---- 工具错误自愈（openspec: tool-error-self-healing） ----
ERROR_MAX_CHARS = 500
RAW_MAX_CHARS = 200
ESCALATE_AT = 2

RETRY_HINTS = {
    "invalid_args": "请检查参数格式，修正为合法 JSON 后重试",
    "unknown_tool": "请从 available 列表选择正确的工具名后重试",
    "timeout": "工具执行超时，可简化任务范围后最多重试一次",
    "transient": "临时网络或系统错误，可稍候重试一次",
    "permanent": "该错误与参数语义或环境相关，请更换方法或参数",
}


def _classify_exception(e: Exception) -> str:
    """异常 → 错误分类（timeout / transient / permanent）"""
    if isinstance(e, (asyncio.TimeoutError, httpx.TimeoutException)):
        return "timeout"
    if isinstance(e, (httpx.HTTPError, OSError)):
        return "transient"
    return "permanent"


def _tool_error(kind: str, msg: str,
                exc: Exception | None = None) -> Dict[str, Any]:
    """统一工具错误信封：非空、截断、分类、修复指引"""
    text = (msg or "").strip() or (
        type(exc).__name__ if exc is not None else kind)
    if len(text) > ERROR_MAX_CHARS:
        text = text[:ERROR_MAX_CHARS]
    return {"success": False, "error": text, "output": "",
            "kind": kind, "retry_hint": RETRY_HINTS.get(kind, "")}

SYSTEM_PROMPT = """你是 MinaClaw，一个具备工具调用能力的智能助手。

工作方式：
1. 分析用户请求，判断是否需要调用工具
2. 如需调用工具，按 OpenAI function call 规范输出 tool_calls
3. 观察工具返回结果，再决定下一步
4. 信息足够后，输出最终答案

工具失败应对：
- 工具返回 success=false 时，先阅读 error 与 retry_hint 再决定下一步
- kind=invalid_args 表示参数格式错误，修正后重新调用；kind=unknown_tool 表示工具名错误，从 available 列表选择正确工具
- timeout / transient 类错误可稍候重试一次；permanent 类请更换方法或参数
- 严禁以完全相同的参数重复调用刚失败的工具；同一工具连续失败时，改变策略或改用其他工具

回答使用中文，结构清晰、内容实用。
"""

if settings.skill_progressive:
    SYSTEM_PROMPT += """
工具渐进式加载：
- 工具列表中的描述仅为摘要
- 首次调用某个工具前，如需了解其详细用法、限制与示例，先调用 load_skill 获取完整说明
- 系统会把本会话已加载技能的完整说明以 [已加载技能] 系统消息注入上下文，已注入的技能无需重复调用 load_skill
"""

class ClawAgent:
    def __init__(self):
        self.tool_schemas: List[dict] = registry.tool_schemas(
            compact=settings.skill_progressive)

    def refresh_tools(self):
        self.tool_schemas = registry.tool_schemas(
            compact=settings.skill_progressive)

    async def run(
        self,
        messages: List[Dict[str, Any]],
        api_key: str,
        session_id: str,
        max_iterations: int | None = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        max_iterations = max_iterations or settings.max_iterations
        tools = self.tool_schemas or None
        # 工具失败防抖：任务级计数（工具名 → 连续失败次数，成功清零）
        fail_counts: Dict[str, int] = {}

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
                args = None
                try:
                    args = json.loads(raw_args)
                except json.JSONDecodeError as e:
                    # 坏参数显式回传，不静默降级执行
                    result = _tool_error(
                        "invalid_args", f"参数不是合法 JSON: {e}", e)
                    result["raw"] = raw_args[:RAW_MAX_CHARS]

                yield {
                    "type": "tool_start",
                    "tool": fn_name,
                    "tool_id": tc["id"],
                    "arguments": args if args is not None else {},
                }

                # 审计
                audit.info("tool_call",
                           extra=log_extra(tool=fn_name, args=args))

                if args is None:
                    pass  # 解析失败已在上方生成错误结果
                else:
                    skill = registry.get(fn_name)
                    if not skill:
                        result = _tool_error(
                            "unknown_tool", f"未知工具: {fn_name}")
                        result["available"] = sorted(registry.skills.keys())
                    else:
                        try:
                            result = await skill.run(
                                args, {"session_id": session_id})
                        except Exception as e:
                            log.exception(
                                "tool run error",
                                extra=log_extra(tool=fn_name))
                            result = _tool_error(
                                _classify_exception(e), str(e), e)

                # ---- 失败防抖：升级提示与终止阈值 ----
                terminate = False
                if result.get("success"):
                    fail_counts[fn_name] = 0
                else:
                    fail_counts[fn_name] = fail_counts.get(fn_name, 0) + 1
                    n_fail = fail_counts[fn_name]
                    if n_fail >= ESCALATE_AT:
                        result["escalation"] = (
                            f"工具 {fn_name} 已连续失败 {n_fail} 次，"
                            "请改变策略或更换工具，不要以相同参数重试")
                    terminate = n_fail >= settings.tool_max_failures

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

                if terminate:
                    yield {
                        "type": "error",
                        "message": (
                            f"工具 {fn_name} 连续失败 "
                            f"{fail_counts[fn_name]} 次，任务终止"),
                    }
                    return

        yield {"type": "done", "reason": "max_iterations"}


agent = ClawAgent()
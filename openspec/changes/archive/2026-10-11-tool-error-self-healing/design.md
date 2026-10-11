# Design

## Context

`core/agent_loop.py` 的 `ClawAgent.run()` 已具备基础回传链路：`skill.run()` 异常被捕获后转 `{"success": False, "error": str(e)}` 以 `role=tool` 消息回传、循环继续。四个断点（见 proposal Why）：147-150 行 `json.loads` 失败静默 `args = {}`；`str(e)` 可空可超长；错误无分类无指引；无失败防抖。`max_iterations=8` 是唯一兜底。错误信封需与既有执行器风格（`skills/skill_executor.py` 各 exec_* 返回 `{"success", "error"}`）及 `load_skill` 的 `available` 列表风格对齐。

## Goals / Non-Goals

**Goals**
- 四层自愈协议全部落地，纯 agent 循环内改动，不触碰执行器与传输层
- 错误结果结构向后兼容（增量字段），前端零改动

**Non-Goals**
- `chat_stream` 网络异常重试（传输层，另行变更）
- 工具统一执行超时（`asyncio.timeout` 包裹，另行变更）
- 前端对 `kind` / `retry_hint` 的 UI 呈现

## Decisions

### D1：统一错误信封与集中构造
模块级纯函数 `_tool_error(kind, msg, exc=None)` 集中生成三种来源（解析失败 / 未知工具 / 执行异常）的错误结果。字段：`success=False`、`kind`、`error`（非空、≤500 字符）、`retry_hint`。空消息回退 `type(exc).__name__`。kind 映射表：`asyncio.TimeoutError` / `httpx.TimeoutException → timeout`；`httpx.HTTPError` / `OSError → transient`；其余 → `permanent`。每类固定 retry_hint 文案。
*备选：在各执行器内自行分类——被否，错误口径会漂移且执行器已有 12 处 try/except，改造面大收益小。*

### D2：解析失败走结果分支而非执行
147-150 行改为：`json.JSONDecodeError` 时构造 `invalid_args` 结果（附 `raw` 原始片段截断 200 字符），跳过 `skill.run`，直接进入 tool_end + tool 消息回传。
*备选：尝试宽松修复（如截尾补括号）——被否，猜测式修复可能产生语义错误的参数，让模型看到真实错误更可靠。*

### D3：失败防抖按工具名计数
`run()` 内局部 `fail_counts: Dict[str, int]`（随任务生命周期，天然隔离，不持久化）。工具失败 `+1`、成功清零。`count >= 2` 时错误结果附加 `escalation` 字段提示改策略；`count >= settings.tool_max_failures` 时 `yield {"type": "error"}` 并 `return`。新增 `TOOL_MAX_FAILURES` 环境变量（默认 3），截断上限 500 固定为模块常量不加配置。
*备选：按「工具名+参数哈希」计数——被否，防抖目标是止损 token 浪费，工具名粒度已足够，参数级匹配易被模型微调参数绕过。*

### D4：SYSTEM_PROMPT 常驻增补
在「工作方式」后追加「工具失败应对」段（约 80 token），不设开关——自愈引导与迭代上限一样属于循环的基础护栏。

### D5：兼容性
`tool_end` 事件与 tool 消息的 result 仅增量字段（`kind` / `retry_hint` / `escalation` / `raw`），`success` / `error` / `output` 语义不变；`tests/` 既有断言不检查错误字段全集，回归风险低。

### D6：测试策略
`tests/test_selfheal.py` 纯内存打桩：monkeypatch `registry.get` / `registry.skills` / `chat_stream`（脚本式轮流返回 tool_call 与最终文本），不依赖 DB / LLM。断言：结果结构（kind/error/retry_hint/raw/available/escalation）、messages 中 role=tool 的内容、事件流（error 终止、迭代次数）。与既有 `tests/test_injection.py` 的打桩风格一致。

## Risks / Trade-offs

- **retry_hint 文案质量影响自愈率**：文案是启发式的，无法保证模型一定按指引修正；接受迭代优化，不改结构。
- **防抖按工具名粒度**：模型轮换不同坏参数可绕过单工具终止；`max_iterations=8` 全局兜底仍生效。
- **`invalid_args` 时 `raw` 回传**：极端情况下模型输出超长垃圾字符串，200 字符截断已限幅。

## Open Questions

无（范围已在提案阶段确定为四层协议，传输层与超时明确排除）。

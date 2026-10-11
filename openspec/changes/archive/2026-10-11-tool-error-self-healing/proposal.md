# Proposal

## Why

Agent 循环中工具异常目前虽然会以 `role=tool` 消息回传模型，但存在四个自愈断点：参数 JSON 解析失败被静默降级为空参数执行（模型不知道自己格式错了，错误信息与根因脱节）；异常消息可能为空或过长（空 error 让模型无从修起，超长错误污染 6000 token 上下文预算）；错误无分类无修复指引（模型不知道该修参数、换工具还是放弃）；同一工具可用相同参数反复失败（8 次迭代可能大半浪费在同一错误上）。本变更按四层自愈协议补齐「异常不中断 + 错误可操作 + 引导修正 + 防抖兜底」的完整链路。

## What Changes

- **参数解析错误显式回传**：tool_call 的 arguments 不是合法 JSON 时不再降级为 `{}` 静默执行，而是构造 `invalid_args` 错误结果（含错误细节、原始片段截断、修复指引）走正常 tool 消息回传，模型下一轮即可修正
- **统一错误信封**：agent 循环内所有工具错误（解析失败 / 未知工具 / 执行异常）由集中构造函数生成，保证错误消息非空（空异常回退异常类名）、有 500 字符截断上限、携带 `kind` 错误分类（`invalid_args` / `unknown_tool` / `timeout` / `transient` / `permanent`）与 `retry_hint` 修复提示
- **未知工具错误附可用列表**：错误结果中给出当前注册的技能名列表，与 `load_skill` 的错误风格对齐，模型可直接改调正确工具
- **失败防抖**：单次任务内按工具名统计连续失败次数——连续失败达到升级阈值（2 次）在错误结果中追加策略提示；达到终止阈值（可配 `TOOL_MAX_FAILURES`，默认 3）则任务以 error 事件终止，不再消耗迭代
- **系统提示词自愈引导**：SYSTEM_PROMPT 增补工具失败应对策略（读错误分类、修参数重试、连续失败换方案、禁止同参重试）

## Capabilities

### New Capabilities
- `tool-error-recovery`: Agent 循环中工具调用异常的自愈行为契约——错误回传协议（结构化错误信封、参数解析错误显式回传、未知工具附可用列表）、失败防抖（升级提示与终止阈值）、以及模型自愈策略引导

### Modified Capabilities

（无：`skill-loading` 现有 6 项需求不受影响，错误自愈是独立行为域）

## Impact

- **代码**：`core/agent_loop.py`（主要：JSON 解析分支、工具执行错误分支、失败计数、SYSTEM_PROMPT）、`config.py`（新增 `TOOL_MAX_FAILURES`）、`.env.example`（同步新环境变量）
- **行为兼容性**：`tool_end` 事件与 role=tool 消息的 result 新增 `kind` / `retry_hint` 字段，`success` / `error` / `output` 语义不变——增量字段，前端透传不受影响，无 **BREAKING**
- **文档**：`README.md` 环境变量表、项目技术文档的工具错误说明
- **测试**：新增 `tests/test_selfheal.py`（打桩假技能验证四层行为），既有 5 个测试脚本回归
- **不涉及**：传输层重试（`chat_stream` 网络异常）、工具统一执行超时——已明确划出范围

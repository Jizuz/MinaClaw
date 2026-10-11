# Spec Delta

## Purpose

Agent 循环的工具异常自愈：工具调用失败时以结构化、可操作的错误信息回传模型，引导其修正参数或更换策略，并以失败防抖防止重复无效调用浪费迭代。

## ADDED Requirements

### Requirement: 参数解析错误回传
tool_call 的 arguments 不是合法 JSON 时，系统 SHALL 不执行该工具，并返回 `success: false`、`kind: invalid_args` 的错误结果（含解析错误细节、原始参数文本的截断片段与修复指引），作为 tool 消息回传供模型修正后重试。

#### Scenario: 非法 JSON 参数不执行工具
- **WHEN** 模型输出 tool_call，其 arguments 字符串不是合法 JSON
- **THEN** 该工具不被执行，错误结果含 `kind: invalid_args`、解析错误细节、原始参数片段（截断）与修复指引

#### Scenario: 修正后的调用正常执行
- **WHEN** 模型根据 `invalid_args` 错误修正参数格式后再次调用同一工具
- **THEN** 参数合法时工具正常执行，错误结果不再出现

### Requirement: 统一错误信封
Agent 循环内工具错误结果 SHALL 由统一结构构成：`success: false`、非空 `error` 文本（异常消息为空时回退为异常类名，超过上限时截断至 500 字符）、`kind` 错误分类（`invalid_args` / `unknown_tool` / `timeout` / `transient` / `permanent`）与 `retry_hint` 修复提示。

#### Scenario: 空消息异常兜底
- **WHEN** 工具执行抛出消息文本为空的异常
- **THEN** 错误结果的 `error` 为该异常的类名而非空字符串

#### Scenario: 超长错误截断
- **WHEN** 工具执行抛出超过 500 字符的错误消息
- **THEN** 回传给模型的 `error` 文本被截断，不污染上下文预算

#### Scenario: 异常按类型分类
- **WHEN** 工具执行分别抛出超时类、临时网络类与其他异常
- **THEN** 错误结果的 `kind` 分别为 `timeout`、`transient`、`permanent`，且各自携带对应修复提示

### Requirement: 未知工具错误附可用列表
模型调用未注册的工具名时，系统 SHALL 返回 `kind: unknown_tool` 的错误结果，并附当前注册的全部技能名列表，供模型直接改调正确工具。

#### Scenario: 调用未注册工具
- **WHEN** 模型调用 registry 中不存在的工具名
- **THEN** 错误结果含 `kind: unknown_tool`、该工具名与可用技能名列表

### Requirement: 失败防抖
单次任务执行内，系统 SHALL 按工具名统计连续失败次数（该工具成功后计数清零）：连续失败达到升级阈值（2 次）时，错误结果 SHALL 附加「已连续失败 N 次，请改变策略」提示；达到终止阈值（环境变量 `TOOL_MAX_FAILURES`，默认 3）时，任务 SHALL 以 error 事件终止，不再消耗后续迭代。

#### Scenario: 连续失败触发策略提示
- **WHEN** 同一工具在同一任务内连续失败 2 次
- **THEN** 第二次的错误结果附加「已连续失败 2 次」的策略变更提示

#### Scenario: 达到终止阈值任务终止
- **WHEN** 同一工具连续失败次数达到 `TOOL_MAX_FAILURES`（默认 3）
- **THEN** 任务以 error 事件终止，不再发起后续 LLM 迭代

#### Scenario: 成功后计数清零
- **WHEN** 连续失败 2 次的工具随后执行成功，再次失败时
- **THEN** 失败计数自本次重新起算（计为 1 次），不与历史失败累计

### Requirement: 自愈策略引导
系统提示词 SHALL 包含工具失败应对策略：阅读错误信息区分参数问题与环境问题、参数问题修正后重试、连续失败更换方法或工具、超时类最多重试一次、禁止以完全相同的参数重复调用刚失败的工具。

#### Scenario: 系统提示词含自愈策略
- **WHEN** 服务组装系统提示词
- **THEN** 提示词中包含工具失败应对策略的关键要点（错误应对、重试约束、策略变更）

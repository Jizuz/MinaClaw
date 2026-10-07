# Proposal

## Why

渐进式加载（`SKILL_PROGRESSIVE`）下，`load_skill` 返回的技能完整详情只存在于当轮 Agent 迭代的内存消息里；会话历史仅持久化 user/assistant 消息（`agent_runner` 只 append 这两种角色），新一轮 `get_context` 重组上下文时上一轮加载的详情已丢失，模型被迫跨轮重复调用 `load_skill` 重新拉取全文。每次重复加载都多消耗一轮 LLM 往返与最高 2 万字符的重复 token——技能越多、会话越长，浪费越大。该需求为 README 路线图既定项「会话级加载缓存」。

## What Changes

- **会话级加载记录**：按 session 记录已通过 `load_skill` 成功加载的技能（技能名 + 加载时的 registry 世代），随会话持久化（复用 `sessions.meta` JSONB，无 DDL 变更），进程重启后可恢复。
- **轮首注入**：新轮组装上下文时，把已加载技能的完整详情作为一条系统消息注入（最近加载优先，受注入字符上限与上下文 token 预算双重约束），模型当轮即可直接使用，无需再调 `load_skill`。
- **命中去重**：`load_skill` 命中「本会话已加载且详情已注入当前上下文」时返回简短提示（`success: true, cached: true`），不再返回全文；若详情未注入当前上下文（被裁剪、超限或开关关闭），则返回完整定义。
- **失效机制**：技能热重载/上传/删除导致 registry 世代变化后，旧记录失效，下一轮按新版本详情重新注入，`load_skill` 亦返回新全文。
- **工具链路透传会话**：Agent 循环执行工具时把 `session_id` 放入工具 `ctx`（当前传空字典，执行器拿不到会话信息）。
- **配置开关**：新增 `SKILL_SESSION_CACHE`（默认 `true`，仅渐进式模式生效）与注入上限 `SKILL_SESSION_CACHE_MAX_CHARS`（默认 4000 字符）。
- **提示词更新**：`SYSTEM_PROMPT` 渐进式说明与 `load_skill` 元工具描述补充「已加载技能无需重复加载」。

## Capabilities

### New Capabilities

- `skill-loading`: 技能渐进式加载能力——tools 仅注入摘要、`load_skill` 元工具按需获取完整详情、会话级加载缓存（记录、轮首注入、命中去重、热重载失效）。

### Modified Capabilities

- 无（项目当前无既有 spec，本变更为首个能力）。

## Impact

- **代码**：`skills/skill_executor.py`（`exec_load_skill` 缓存判定）、`skills/skill_loader.py`（`META_TOOL_DESC`、registry 世代计数）、`core/agent_loop.py`（ctx 透传 session_id、`SYSTEM_PROMPT`）、`core/session_manager.py`（loaded_skills 记录、`get_context` 轮首注入、`sessions.meta` 读写）、`config.py`（新配置项）。
- **存储**：复用 `sessions.meta` JSONB 列，无 DDL 变更。
- **API/协议**：无新增或破坏性端点；WebSocket 事件流不变，`load_skill` 工具结果新增 `cached` 字段（向后兼容）。
- **文档**：README 路线图勾选该项、`docs/skills-api.md` 补充行为说明。

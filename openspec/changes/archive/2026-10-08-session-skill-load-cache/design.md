# Design

## Context

- 渐进式加载链路：`registry.tool_schemas(compact=True)` 注入摘要 + `load_skill` 元工具（`skills/skill_loader.py`）；`exec_load_skill`（`skills/skill_executor.py`）返回 `skill.detail_text()`（截断上限 20000 字符）。
- 会话链路：`agent_runner`（`main.py`）→ `session_manager.append(user)` → `get_context()`（`core/session_manager.py`，token 预算裁剪 + 摘要压缩写回）→ `agent.run()`（`core/agent_loop.py`，ReAct 循环）。tool 结果仅存在于当轮 `messages`，不持久化——这是跨轮重复加载的根因。
- 工具执行 ctx 为空字典（`core/agent_loop.py` 中 `skill.run(args, {})`），执行器拿不到 `session_id`。
- `sessions.meta` JSONB 列已存在未使用；`db/pool.py` 提供 psycopg3 的 `execute` 薄封装。
- 技能热加载入口（上传 / 删除 / `/api/skills/reload`）统一走 `registry.load_all()` + `agent.refresh_tools()`。
- 方案已与用户确认为「轮首注入 + 命中去重」（否决「仅同轮去重」——跨轮详情丢失后仍会重复加载；否决「持久化 tool 消息」——改动会话存储与裁剪配对逻辑，范围过大）。

## Goals / Non-Goals

**Goals:**

- 跨轮消除 `load_skill` 重复加载：既省重复注入的 token，也省一次 LLM 往返
- 记录随会话持久化，进程重启可恢复；技能热更新后自动失效
- 注入受上下文预算约束，不破坏现有历史裁剪与摘要压缩行为
- 渐进式模式或缓存开关任一关闭时，行为完全退化为现状

**Non-Goals:**

- 不持久化 tool 消息到会话历史（不改动 messages 表写入与裁剪的 tool_call 配对逻辑）
- 不做跨会话/全局缓存、向量检索技能路由（README 其余路线图项）
- 不改变 WebSocket 事件协议与 REST API 形状（`load_skill` 结果新增 `cached` 字段为向后兼容增量）

## Decisions

### D1: 缓存状态放 SessionManager，持久化用 sessions.meta（jsonb_set）

- 会话 dict 增加 `loaded_skills`（有序列表 `[{"name": ..., "gen": ...}]`，最近在后）与 `injected_skills`（集合，`get_context` 时整体重建，代表「详情已进入本轮上下文」）。
- `_load_history` 的 SELECT 增加 `meta` 列，创建缓存时恢复记录；`create` 初始化为空。
- 写穿：`UPDATE sessions SET meta = jsonb_set(COALESCE(meta, '{}'::jsonb), '{loaded_skills}', %s::jsonb) WHERE id = %s`，保留 meta 中其他键。
- 备选：独立 `core/skill_cache.py` 模块（拒绝：会话生命周期与持久化割裂，仍需读写 sessions 表）；Redis（拒绝：引入新依赖，内存 + PG 写穿已满足单机架构）。

### D2: 失效判定用 registry 世代计数

- `SkillRegistry` 增加 `generation`（int，初始 0），`load_all()` 末尾自增——上传/删除/重载全部经由 `load_all()`，一个计数覆盖所有失效入口。
- 记录有效性 = 技能仍注册 且 记录 `gen == registry.generation`；不等（含重启后 `gen` 大于归零后的当前值）一律判失效，保守回退为重新加载。
- 备选：逐技能 mtime/内容哈希版本（拒绝：需要为每个 Skill 维护版本字段与比对逻辑，世代计数以最小成本达到同等失效语义）。

### D3: 轮首注入在 get_context 内完成，并计入 token 预算

- 位置：主 system prompt 与 `[历史摘要]` 之后、历史消息之前，作为一条独立 system 消息，前缀 `[已加载技能]`，正文为按最近优先排列的 `## <name>` + `detail_text()`，并注明「无需再次调用 load_skill」。
- 双重约束：先按 `SKILL_SESSION_CACHE_MAX_CHARS`（默认 4000 字符）从最近到最早截取详情；注入消息 tokens 并入 `used` 后再计算历史可选预算；超出字符上限的技能仅列名并提示「可重新 load_skill 获取」。
- 开关：`settings.skill_session_cache` 为 false 或非渐进式模式时跳过注入（`injected_skills` 置空）。
- 备选：在 `agent_runner` 组装后追加注入（拒绝：绕过 `get_context` 的预算核算，注入挤占历史窗口不受控）。

### D4: exec_load_skill 经 ctx 获取 session_id，命中判定读当轮注入集合

- `agent_loop` 执行工具时传 `ctx = {"session_id": session_id}`；`Skill.run` 已将 ctx 与 `_skill` 合并，其余执行器忽略未知键，无兼容风险。
- `exec_load_skill` 四分支：
  1. 无 `session_id`、开关关闭或非渐进式 → 维持现状：返回全文，不记录。
  2. 记录缺失或失效（技能被删 / 世代不符）→ 返回全文，按当前世代（重新）记录并置顶最近性。
  3. 记录有效且技能 ∈ 当轮 `injected_skills` → 返回 `{"success": True, "cached": True, "skill": name, "output": "该技能完整说明已在当前上下文中（系统注入），无需重复加载。"}`。
  4. 记录有效但未注入（超限列名 / 被裁剪）→ 返回全文并置顶最近性（下一轮优先注入）。
- 同轮内重复调用自然命中分支 3：`get_context` 建立的 `injected_skills` 覆盖整个 `agent.run` 生命周期。

### D5: 提示词与元工具描述同步更新

- `META_TOOL_DESC` 与 `SYSTEM_PROMPT` 渐进式段落补充：「系统会把本会话已加载技能的完整说明注入上下文；已注入的技能不要重复调用 load_skill」。

## Risks / Trade-offs

- [同会话并发任务读写 `injected_skills` 竞态] → 注入集合在 `get_context` 中一次性整体替换（原子赋值），读侧仅做成员判断；风险等级与现有 history 并发 append 一致。
- [注入挤占上下文预算（`max_context_tokens` 默认 6000），历史被多裁] → 注入计入 `used` 且有独立字符上限（默认 4000 字符）；超限时详情让位于最近历史（列名兜底）；可调低 `SKILL_SESSION_CACHE_MAX_CHARS` 或关闭开关。
- [世代失效后短暂重复加载] → 预期中的一次性成本，换来技能热更新后模型必然拿到新版说明。
- [meta 写穿失败导致内存与库漂移] → 写穿失败仅记日志不阻断对话；下次成功加载时以内存为准整体重写 `loaded_skills` 路径自愈。
- [重启后 generation 归零而旧记录 `gen` 残留] → 有效性判定要求严格相等，不等即失效，保守回退为重新加载一次。

## Migration Plan

1. 纯增量变更：无 DDL、无 API 破坏；`SKILL_SESSION_CACHE` 默认开启，但仅渐进式模式生效。
2. 部署即生效；回滚 = 设置 `SKILL_SESSION_CACHE=false`（行为退化为现状）或回退代码，旧 `sessions.meta` 数据无需清理。
3. 存量会话 `meta` 为 NULL：`COALESCE` 兜底为空对象，首次 `load_skill` 后自然写入。

## Open Questions

- 注入的系统消息是否需要在前端 UI 显式展示：当前设计不新增 WebSocket 事件（`load_skill` 结果中的 `cached` 字段已可观测）；若前端需要「本会话已加载技能」面板，后续单独扩展。

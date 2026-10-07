# Tasks

> 验证说明：项目无 pytest，验证用独立 Python 脚本（放 `tests/`，直接以 claw venv 的 `python3` 运行，DB 访问以打桩替换 `db.pool` 模块函数）；端到端场景按 `python3 main.py`（或 uvicorn）启动后经 WebSocket 手工验证。

## 1. 配置与注册表世代

- [x] 1.1 `config.py` 新增 `skill_session_cache`（env `SKILL_SESSION_CACHE`，默认 `true`）与 `skill_session_cache_max_chars`（env `SKILL_SESSION_CACHE_MAX_CHARS`，默认 `4000`），并同步 `.env.example`；验证：`python3 -c "from config import settings; assert settings.skill_session_cache is True and settings.skill_session_cache_max_chars == 4000; print('config ok')"`
- [x] 1.2 `skills/skill_loader.py` 的 `SkillRegistry` 增加 `generation` 计数，`load_all()` 每次调用自增；验证：`python3 tests/test_generation.py` 断言连调两次 `load_all()` 后 `generation` 递增且初值为 0

## 2. 会话加载记录与持久化

- [x] 2.1 `core/session_manager.py` 会话 dict 增加 `loaded_skills`（有序列表，元素 `{name, gen}`）与 `injected_skills`（集合），`create` 初始化为空、`_load_history` 的 SELECT 增加 `meta` 列并恢复记录（`meta` 为 NULL 时兜底为空）；验证：`python3 tests/test_session_record.py` 打桩 db 后断言记录恢复与 NULL 兜底
- [x] 2.2 实现写穿方法：`record_skill_load(sid, name, gen)`（去重、置顶最近性、`jsonb_set(COALESCE(meta,'{}'::jsonb), '{loaded_skills}', ...)` 写库）与 `clear_skill_load(sid, name)`（移除并写库）；验证：`python3 tests/test_session_record.py` 断言排序、去重与 UPDATE SQL 及参数正确

## 3. 轮首注入与 ctx 透传

- [x] 3.1 `get_context` 组装 `[已加载技能]` 系统消息：仅渐进式模式且 `skill_session_cache` 开启时注入；最近优先、按 `skill_session_cache_max_chars` 截断详情、超限技能仅列名提示；注入消息 tokens 计入 `used` 再裁剪历史；同时把本轮实际注入的技能名写入 `injected_skills`；验证：`python3 tests/test_injection.py` 断言注入内容、预算扣减、超限列名与开关关闭时不注入
- [x] 3.2 `core/agent_loop.py` 工具执行改为 `skill.run(args, {"session_id": session_id})`；验证：`python3 -m py_compile core/agent_loop.py` 且 `python3 tests/test_injection.py` 含一个用 ctx 记录 `_skill` 与 `session_id` 的假执行器冒烟断言
- [x] 3.3 `get_context` 注入前按 D2 校验记录有效性（技能仍注册且 `gen == registry.generation`），无效记录跳过注入并在内存中剔除；验证：`python3 tests/test_injection.py` 断言世代不符/技能删除的记录不注入

## 4. load_skill 命中去重

- [x] 4.1 `exec_load_skill` 实现四分支（无会话或开关关闭→现状全文；记录缺失/失效→全文并按当前世代重记；命中 `injected_skills`→`{"success": true, "cached": true}` 简短提示；有效但未注入→全文并置顶最近性）；验证：`python3 tests/test_load_skill_cache.py` 打桩后逐一断言四分支的返回结构与记录变化
- [x] 4.2 热重载失效联动：上传/删除技能触发 `load_all()` 世代变化后，旧记录判无效、再次 `load_skill` 返回新全文并重记、已删除技能记录被清除；验证：`python3 tests/test_load_skill_cache.py` 模拟覆盖上传与删除后断言行为符合 spec 场景

## 5. 提示词与文档

- [x] 5.1 更新 `META_TOOL_DESC` 与 `SYSTEM_PROMPT` 渐进式段落，补充「已注入技能无需重复调用 load_skill」；验证：`python3 -c "from core.agent_loop import SYSTEM_PROMPT; from skills.skill_loader import META_TOOL_DESC; assert '无需' in SYSTEM_PROMPT or '无需' in META_TOOL_DESC; print('prompt ok')"`
- [x] 5.2 更新 `README.md`（路线图勾选「会话级加载缓存」、环境变量表新增 `SKILL_SESSION_CACHE` / `SKILL_SESSION_CACHE_MAX_CHARS`）与 `docs/skills-api.md`（轮首注入、命中去重、失效行为说明）；验证：文档中的变量名与 `config.py` 默认值逐字一致

## 6. 集成验证

- [x] 6.1 端到端验证：启动服务，同一会话 WebSocket 两轮对话——首轮 `load_skill` 返回全文，次轮上下文含 `[已加载技能]` 系统消息、再次 `load_skill` 返回 `cached: true`；验证：日志（`load_skill` 带 `cached`）与 WS `tool_end` 事件确认两轮行为符合预期
- [x] 6.2 边界验证：重启服务后同会话继续对话（记录恢复注入）；重新上传已加载技能（旧记录失效、返回新全文）；`SKILL_SESSION_CACHE=false`（不注入、`load_skill` 恒返回全文）；验证：三种场景逐一观察日志/事件确认与 spec 场景一致

## Workflow follow-up

- 全部任务完成后运行 `PATH=/usr/local/bin:$PATH openspec validate session-skill-load-cache`（CLI 需 Node ≥18.20，本机用 `/usr/local/bin` 的 Node v24）校验通过
- 归档：审阅通过后走 `/opsx-archive`（`openspec archive session-skill-load-cache`），将 `specs/skill-loading/spec.md` 合入主 spec

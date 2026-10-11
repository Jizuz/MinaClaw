# Tasks

## 1. 错误信封与解析错误回传

- [x] 1.1 测试先行（红）：`tests/test_selfheal.py` 打桩 `chat_stream` 返回坏 JSON arguments 的 tool_call，断言工具不执行、结果含 `kind: invalid_args` / 非空 `error` / `raw` 截断片段 / `retry_hint`；另断言空消息异常时 `error` 为异常类名、超长错误截断至 500 字符；验证：`./claw/bin/python3 tests/test_selfheal.py` 相关用例因实现缺失而失败
- [x] 1.2 实现（绿）：`core/agent_loop.py` 新增模块级 `_tool_error()` 统一错误信封（空消息回退类名、500 字符截断），`json.loads` 失败改走 `invalid_args` 结果分支并跳过执行；验证：1.1 用例全部转绿

## 2. 未知工具与异常分类

- [x] 2.1 测试先行（红）：断言调用未注册工具返回 `kind: unknown_tool` 且附 `available` 技能名列表；断言 `asyncio.TimeoutError`→`timeout`、`httpx.HTTPError`→`transient`、其余→`permanent` 的 kind 映射及各自 `retry_hint`；验证：新增用例先失败
- [x] 2.2 实现（绿）：未知工具分支接入 `_tool_error` 并附可用列表，执行异常分支按 D1 映射表分类；验证：2.1 用例转绿

## 3. 失败防抖

- [x] 3.1 测试先行（红）：同一工具连续失败 2 次断言第二次结果含 `escalation` 策略提示；连续失败 3 次断言收到 `error` 事件且无第 4 次 LLM 迭代；失败 2 次后成功再失败断言计数清零；验证：新增用例先失败
- [x] 3.2 实现（绿）：`run()` 内 `fail_counts` 按工具名计数（成功清零、阈值升级提示、达 `settings.tool_max_failures` 以 error 事件终止）；验证：3.1 用例转绿

## 4. 配置与提示词

- [x] 4.1 `config.py` 新增 `tool_max_failures`（环境变量 `TOOL_MAX_FAILURES`，默认 3），`.env.example` 同步；验证：`./claw/bin/python3 -c "from config import settings; assert settings.tool_max_failures == 3; print('config ok')"`
- [x] 4.2 `SYSTEM_PROMPT` 增补「工具失败应对」段（读错误分类、修参数重试、连续失败换方案、禁止同参重试）；验证：断言提示词含关键要点短语

## 5. 文档与回归

- [x] 5.1 `README.md` 环境变量表新增 `TOOL_MAX_FAILURES`，技术文档补工具错误信封字段说明（`kind` / `retry_hint` / `escalation`）；验证：文档变量名与默认值与 `config.py` 逐字一致
- [x] 5.2 全量回归：`tests/` 下全部 6 个测试脚本运行通过（含既有 5 个无破坏）；验证：逐个执行并确认输出 ok

## Workflow follow-up

- 审阅通过后执行 `/opsx-archive tool-error-self-healing`（含 delta spec 同步至 `openspec/specs/tool-error-recovery/spec.md`）

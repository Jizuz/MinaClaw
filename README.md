# MinaClaw

> 基于 FastAPI 的轻量级 **ReAct Agent 网关**：WebSocket 流式对话 + 插件化技能系统 + 沙箱执行 + 流量治理 + Token 预算控制。

MinaClaw 通过 OpenAI 兼容协议对接大模型（默认智谱 GLM，可切换 OpenAI / DeepSeek / vLLM / Ollama 等），将 LLM 的 `tool_calls` 能力封装为一套安全、可观测、可限流的 Agent 服务。

## 特性

- **WebSocket 流式对话** — 逐 token 推送思考过程、工具调用、最终答案，支持中断（interrupt）与心跳（ping/pong）
- **ReAct Agent 循环** — 基于 OpenAI Function Calling 协议，最多 8 轮"推理 → 调用工具 → 观察"迭代
- **Agent Skills 标准技能** — 目录化 `SKILL.md`（front-matter 对齐 Agent Skills 开放标准），热加载，无需重启；三级渐进披露：摘要常驻注入，正文与附属文件经 `load_skill` 元工具按需获取
- **沙箱安全** — 文件读写限制在沙箱目录内（路径逃逸 / 符号链接 / 扩展名 / 大小四重检查）；bash 仅放行白名单命令
- **流量治理** — 滑动窗口 QPS（全局 + 单 Key）、令牌桶突发控制、并发上限、熔断器（closed / open / half-open 三态）
- **任务队列** — 有界优先队列 + 固定 worker 池 + 单任务超时 + 取消
- **Token 预算** — tiktoken 精确计量，全局 / 按 Key / 按会话三级统计，每日配额与预警阈值，用量落盘 `logs/tokens.jsonl`
- **上下文管理** — 超出 token 预算自动压缩历史为摘要（LLM 摘要，失败时降级为截断式摘要）
- **结构化日志** — JSON 行格式、按天滚动、审计日志独立落盘，`contextvars` 注入 request_id / session_id / task_id

## 架构

```
┌──────────┐   WebSocket    ┌─────────────────────────────────────┐
│  Client  │ ◄────────────► │  FastAPI Gateway (main.py)          │
└──────────┘                │                                     │
                            │  auth ──► rate_limit ──► task_queue │
                            │                                      │
                            │             agent_loop (ReAct)      │
                            │                 │                    │
                            │        ┌────────┴────────┐          │
                            │        ▼                 ▼          │
                            │  openai_client      skill_registry  │
                            │  (流式+usage)      (defs/*.md 插件)  │
                            │                        │            │
                            │                        ▼            │
                            │                 skill_executor      │
                            │                 (bash/文件/邮件…)    │
                            └─────────────────────────────────────┘
```

```
MinaClaw/
├── main.py                 # 入口：FastAPI 应用、lifespan、agent_runner
├── config.py               # pydantic-settings 配置（读 .env）
├── api/                    # 路由层
│   ├── routes/
│   │   ├── agent.py        # WebSocket 对话 + 流量/队列/Token 统计
│   │   ├── session.py      # 会话创建与列表
│   │   └── skills.py       # 技能列表与热重载
│   └── schemas.py
├── core/                   # 核心逻辑
│   ├── agent_loop.py       # ReAct 循环（流式 + 工具调用编排）
│   ├── openai_client.py    # OpenAI 兼容流式客户端
│   ├── session_manager.py  # 内存会话 + 上下文预算裁剪
│   ├── memory_compactor.py # LLM 记忆压缩器
│   └── task_queue.py       # 有界优先队列 + worker 池
├── middleware/
│   ├── auth.py             # X-API-Key 鉴权
│   └── rate_limit.py       # QPS / 令牌桶 / 并发 / 熔断
├── skills/
│   ├── defs/               # 技能定义（Agent Skills 标准：<name>/SKILL.md）
│   ├── base_skill.py
│   ├── skill_loader.py     # front-matter 解析 + 注册表
│   └── skill_executor.py   # 各执行器（bash/文件/邮件/http…）
├── utils/
│   ├── sandbox.py          # 文件沙箱（四重安全检查）
│   ├── token_counter.py    # tiktoken 计量 + 每日配额
│   └── logger.py           # 结构化日志 + 审计日志
├── sandbox/                # 运行时沙箱目录（自动创建，不入库）
└── logs/                   # 运行日志（自动创建，不入库）
```

## 快速开始

### 环境要求

- Python 3.10+（开发环境为 3.13）

### 安装

```bash
git clone https://github.com/Jizuz/MinaClaw.git
cd MinaClaw

python -m venv claw
source claw/bin/activate        # Windows: claw\Scripts\activate

pip install -r requirements.txt
```

### 配置

```bash
cp .env.example .env
# 编辑 .env，填入你的 LLM API Key
```

<details>
<summary><b>完整配置项</b></summary>

| 变量 | 说明 | 默认值 |
|---|---|---|
| `OPENAI_API_KEY` | LLM 服务商 API Key | — |
| `OPENAI_BASE_URL` | OpenAI 兼容端点 | `https://open.bigmodel.cn/api/paas/v4` |
| `OPENAI_MODEL` | 模型名 | `glm-4-plus` |
| `DATABASE_URL` | PostgreSQL 连接串（`jdbc:postgresql://` 或 `postgresql://`） | `jdbc:postgresql://localhost:5432/mina` |
| `JWT_SECRET` | 登录 JWT 签名密钥（生产环境务必修改） | `change-me-in-production` |
| `JWT_EXPIRE_MINUTES` | JWT 有效期（分钟） | `1440` |
| `MINACLAW_API_KEYS` | 网关静态鉴权 Key（逗号分隔多个，向后兼容） | `dev-key-123` |
| `LOG_LEVEL` / `LOG_DIR` / `LOG_JSON` | 日志级别 / 目录 / JSON 开关 | `INFO` / `./logs` / `true` |
| `DAILY_TOKEN_QUOTA` | 每日 Token 配额 | `1000000` |
| `DAILY_TOKEN_WARN` | Token 预警阈值 | `800000` |
| `SMTP_HOST` 等 | 邮件技能 SMTP 配置 | — |
| `SKILL_PROGRESSIVE` | 技能渐进式加载（摘要注入 + `load_skill` 按需取详情） | `true` |
| `SKILL_UPLOAD_*` | 技能上传：开关 / 压缩包上限 MB / 解压总量上限 MB / 最大文件数 | `true` / `5` / `20` / `50` |

</details>

### 运行

```bash
python main.py
# 或
uvicorn main:app --host 0.0.0.0 --port 8010
```

- 服务地址：`http://localhost:8010`
- 交互式 API 文档（Swagger）：`http://localhost:8010/docs`

## API

认证方式（三选一，按优先级）：

1. **JWT（推荐）**：`POST /api/auth/login` 登录后携带 `Authorization: Bearer <token>`
2. **数据库 API Key**：`POST /api/keys` 创建后携带 `X-API-Key: mc_xxx`（库中只存 sha256，支持吊销/过期）
3. **静态 Key**：`MINACLAW_API_KEYS` 环境变量（向后兼容 `dev-key-123`，无归属用户）

> 前端对接：技能管理（列表 / 上传 / 删除）接口的完整请求响应示例、TypeScript 类型与 Client 封装见 **[docs/skills-api.md](docs/skills-api.md)**。

### REST

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/auth/register` | 注册用户（username + password，可选 display_name / email） |
| `POST` | `/api/auth/login` | 登录，返回 JWT 与用户信息 |
| `GET` | `/api/auth/me` | 当前登录用户信息 |
| `POST` | `/api/keys` | 创建 API Key（明文 `mc_` 前缀仅返回一次） |
| `GET` | `/api/keys` | 列出当前用户的 API Key |
| `DELETE` | `/api/keys/{id}` | 吊销 API Key（立即失效） |
| `POST` | `/api/session/create` | 创建会话，返回 `session_id` |
| `GET` | `/api/session/list` | 当前用户会话列表（含标题/统计） |
| `GET` | `/api/session/{sid}/messages` | 会话历史消息（校验归属） |
| `GET` | `/api/skills/list` | 已加载技能及其参数 Schema |
| `POST` | `/api/skills/reload` | 热重载技能插件 |
| `POST` | `/api/skills/upload` | 上传 zip 技能目录包（multipart，校验通过即热加载） |
| `DELETE` | `/api/skills/{name}` | 删除技能并热加载 |
| `GET` | `/api/chat/traffic` | 流量统计（QPS / 并发 / 熔断状态） |
| `GET` | `/api/chat/queue` | 任务队列统计（等待 / 运行 / 平均等待时长） |
| `GET` | `/api/chat/tokens?session_id=` | Token 用量（全局 / Top Key / Top 会话 / 按小时） |

### WebSocket 对话

```
ws://localhost:8010/api/chat/ws?session_id=<sid>&token=<jwt>
# 或
ws://localhost:8010/api/chat/ws?session_id=<sid>&api_key=<key>
```

会话与消息持久化到 PostgreSQL（`sessions` / `messages` 表，按用户隔离），服务重启后上下文自动恢复。

**客户端 → 服务端**

| 消息 | 说明 |
|---|---|
| `{"type": "message", "content": "..."}` | 发送用户消息（进入限流检查 → 配额检查 → 任务队列） |
| `{"type": "interrupt"}` | 取消当前正在执行的任务 |
| `{"type": "ping"}` | 心跳，服务端回 `pong` |

**服务端 → 客户端**（事件流）

| 事件 | 说明 |
|---|---|
| `ready` | 连接就绪（会话不存在时自动创建） |
| `queued` | 任务已入队，含队列快照 |
| `thinking` | 模型流式输出增量 `delta` |
| `tool_start` / `tool_end` | 工具调用开始 / 结束（含参数与结果） |
| `usage` | 本轮 Token 消耗（prompt / completion / 轮次） |
| `done` | 回答完成（或达到最大迭代次数） |
| `error` / `cancelled` | 错误 / 已取消（含限流、配额超限原因） |

## 技能系统

技能定义遵循 **Agent Skills 开放标准**：目录 `skills/defs/<skill-name>/SKILL.md`，front-matter 仅含标准字段；MinaClaw 扩展配置写在正文首个 ` ```yaml ` 围栏块中，解析时自动提取，不进入给 LLM 的说明文字。修改后调用 `POST /api/skills/reload` 即可热加载。

| 字段 | 位置 | 约束 / 说明 |
|---|---|---|
| `name` | front-matter | `^[a-z0-9-]+$`（小写字母/数字/连字符），≤64 字符，目录形式须与目录名一致 |
| `description` | front-matter | ≤1024 字符，第三人称、说明何时使用（用于渐进式加载的摘要注入） |
| `license` / `metadata` / `allowed-tools` | front-matter | 可选标准字段，解析后透传（`allowed-tools` 暂不生效） |
| `executor` / `parameters` / `executor_args` / `template` 等 | 正文 yaml 配置块 | MinaClaw 扩展：执行器路由 + OpenAI JSON Schema |

````markdown
---
name: file-read
description: 读取沙箱内的文本文件内容。当用户需要查看某个文件时使用。
---

```yaml
executor: file_read          # 对应 skill_executor 中的执行器
parameters:                  # OpenAI JSON Schema
  type: object
  properties:
    path:
      type: string
      description: 沙箱内相对路径
  required: [path]
```

补充给模型的工具使用说明，随 load_skill 详情返回。
````

**三级渐进披露**（默认开启，`SKILL_PROGRESSIVE=false` 回退全量注入）：

| 层级 | Agent Skills 标准 | MinaClaw 实现 |
|---|---|---|
| Level 1 | name + description 常驻 | 每轮请求 `tools` 仅注入摘要（description + 参数 Schema），prompt token 不随正文膨胀 |
| Level 2 | SKILL.md 全文 | 内置元工具 `load_skill(skill="<name>")` 返回 description + 正文，作为 tool result 进入当轮上下文 |
| Level 3 | 捆绑资源按需读取 | SKILL.md 同目录附属文件（references/ 等）随 `load_skill` 一并内联返回（上限 20000 字符） |

**兼容性**

- 旧格式单文件 `defs/*.md`（executor/parameters 直接写在 front-matter）仍可加载（记 deprecation 警告）；目录形式优先
- 若定义了与内置元工具同名的 `load_skill` 技能，自动回退全量注入并记录警告
- 纯提示词技能（无 executor）不支持——MinaClaw 为 function calling 网关，executor 必填

**上传与删除**（免改文件系统的运维通道，`SKILL_UPLOAD_ENABLED=false` 可整体关闭）

- `POST /api/skills/upload`：multipart 上传 zip 目录包（`file` 字段，可选 `overwrite=true`）。包内须**恰好一个技能**，两种布局均可：根级 `SKILL.md`（打包目录内容）或 `<name>/SKILL.md`（打包目录本身）。校验通过后落位 `defs/<name>/` 并自动热加载，响应返回技能摘要（`name` / `description` / `executor` / `assets` / `overwritten`）
- `DELETE /api/skills/{name}`：删除技能目录（兼容旧单文件）并热加载；内置元工具 `load_skill` 不可删除
- 安全治理：zip-slip 路径穿越防护；zip bomb 限额（压缩包 ≤5MB、解压后 ≤20MB、≤50 个文件，`SKILL_UPLOAD_MAX_*` 可调）；附属文件扩展名白名单（md/txt/json/csv/yaml/yml/html/css/js/py/log）；隐藏与系统条目（`.DS_Store`、`__MACOSX`）自动跳过；executor 必须在注册表内；`load_skill` 名与 `skill_loader` 执行器为系统保留
- 同名冲突默认返回 409，传 `overwrite=true` 覆盖（旧目录先备份、失败自动回滚）；任何校验失败返回 400/413，不落盘任何文件

**内置技能**

| 技能 | 执行器 | 说明 |
|---|---|---|
| `bash` | bash | 沙箱内执行白名单 shell 命令（无管道/重定向/替换，10s 超时） |
| `file-list` / `file-read` / `file-write` | file | 沙箱内文件列表 / 读取 / 写入 |
| `send-email` | smtp | 发送邮件（aiosmtplib） |
| `design-travel-plan` | template | 生成旅行攻略框架（含 references/prefs.md 附属资源） |

> v2.1 起技能命名由下划线改为连字符以对齐开放标准（如 `file_read` → `file-read`）。

## 安全与治理

**文件沙箱**（`utils/sandbox.py`）

- 路径逃逸检查（resolve 后必须位于沙箱根内）
- 符号链接指向沙箱外直接拒绝
- 扩展名白名单：`.txt .md .json .csv .py .js .html .css .yaml .yml .log`
- 单文件写入上限 10MB

**限流与熔断**（`middleware/rate_limit.py`）

| 项 | 默认值 |
|---|---|
| 全局 QPS（60s 滑动窗口） | 50 |
| 单 Key QPS | 10 |
| 突发容量（令牌桶，全局/单Key） | 100 / 20 |
| 最大并发 | 20 |
| 熔断阈值 / 冷却时间 | 连续 10 次失败 / 30s |

**任务队列**（`core/task_queue.py`）：容量 200、4 个 worker、单任务 300s 超时，支持按 task_id / session_id 取消。

**Token 预算**：请求前预检配额，调用后按 API 返回的 usage 精确计量（缺失时 tiktoken 兜底估算），超限返回 `token_quota_exceeded`。

## 可观测性

- `logs/app.log.*` — 应用日志（JSON 行格式，按天滚动）
- `logs/audit.log.*` — 审计日志（用户消息、工具调用与结果、Token 消耗）
- `logs/tokens.jsonl` — 每次模型调用一行用量记录（Key 自动脱敏）
- 运行时指标：`/api/chat/traffic`、`/api/chat/queue`、`/api/chat/tokens`

## 未来可扩展方向

### 技能系统

- **技能仓库与远程加载** — 在现有 zip 上传接口基础上，支持从 Git 仓库 / URL 拉取技能目录包，实现技能集市与版本管理（配合签名校验保障供应链安全）
- **向量检索技能路由** — 技能数量进一步增长时，先按用户意图检索 Top-K 技能摘要再注入 `tools`，与渐进式加载叠加，使 token 占用不随技能总数线性膨胀
- **技能编排** — 技能间依赖声明与组合调用（workflow），支持一次完成多步骤任务
- **会话级加载缓存** — 在会话中记录已 `load_skill` 的技能，避免跨轮重复加载

### 记忆与上下文

- **长期记忆** — 引入向量存储（如 Chroma / Qdrant），沉淀跨会话用户偏好与事实，替代当前截断式摘要
- **分层上下文管理** — 工作记忆 / 情景记忆 / 语义记忆分层，按相关性召回而非仅按 token 预算裁剪

### 协议与生态

- **MCP 兼容** — 将技能暴露为 Model Context Protocol 标准工具，供 Claude Desktop / 其他 Agent 框架直接复用
- **SSE 流式接口** — 在 WebSocket 之外提供 HTTP SSE 通道，降低客户端接入门槛
- **多 Agent 协作** — supervisor / handoff 模式，主 Agent 将子任务分派给专职子 Agent

### 安全与治理

- **容器级沙箱** — bash 技能升级为 Docker / gVisor 隔离执行，替代当前命令白名单
- **工具人审（HITL）** — 高危技能（发邮件、写文件）执行前挂起，等待人工确认后放行
- **按 Key 技能授权** — 细粒度 ACL，不同 API Key 可见 / 可调用不同技能集

### 运维与可观测

- **状态持久化** — 会话与 Token 计量落 Redis / PostgreSQL，支持重启不丢失与多实例共享
- **Prometheus / OpenTelemetry** — 标准指标暴露与分布式链路追踪
- **轨迹回放与评估** — 审计日志结构化升级，支持 Agent 轨迹回放、回归评估（evals）与不同模型 / 提示词的 A/B 对比

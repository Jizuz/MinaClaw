# MinaClaw 🦀

> 基于 FastAPI 的轻量级 **ReAct Agent 网关**：WebSocket 流式对话 + 插件化技能系统 + 沙箱执行 + 流量治理 + Token 预算控制。

MinaClaw 通过 OpenAI 兼容协议对接大模型（默认智谱 GLM，可切换 OpenAI / DeepSeek / vLLM / Ollama 等），将 LLM 的 `tool_calls` 能力封装为一套安全、可观测、可限流的 Agent 服务。

## ✨ 特性

- **WebSocket 流式对话** — 逐 token 推送思考过程、工具调用、最终答案，支持中断（interrupt）与心跳（ping/pong）
- **ReAct Agent 循环** — 基于 OpenAI Function Calling 协议，最多 8 轮"推理 → 调用工具 → 观察"迭代
- **Markdown 技能插件** — 技能以 `YAML front-matter + Markdown` 定义，热加载，无需重启
- **沙箱安全** — 文件读写限制在沙箱目录内（路径逃逸 / 符号链接 / 扩展名 / 大小四重检查）；bash 仅放行白名单命令
- **流量治理** — 滑动窗口 QPS（全局 + 单 Key）、令牌桶突发控制、并发上限、熔断器（closed / open / half-open 三态）
- **任务队列** — 有界优先队列 + 固定 worker 池 + 单任务超时 + 取消
- **Token 预算** — tiktoken 精确计量，全局 / 按 Key / 按会话三级统计，每日配额与预警阈值，用量落盘 `logs/tokens.jsonl`
- **上下文管理** — 超出 token 预算自动压缩历史为摘要（LLM 摘要，失败时降级为截断式摘要）
- **结构化日志** — JSON 行格式、按天滚动、审计日志独立落盘，`contextvars` 注入 request_id / session_id / task_id

## 🏗 架构

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
│   ├── defs/               # 技能定义（Markdown 插件）
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

## 🚀 快速开始

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
| `MINACLAW_API_KEYS` | 网关鉴权 Key（逗号分隔多个） | `dev-key-123` |
| `LOG_LEVEL` / `LOG_DIR` / `LOG_JSON` | 日志级别 / 目录 / JSON 开关 | `INFO` / `./logs` / `true` |
| `DAILY_TOKEN_QUOTA` | 每日 Token 配额 | `1000000` |
| `DAILY_TOKEN_WARN` | Token 预警阈值 | `800000` |
| `SMTP_HOST` 等 | 邮件技能 SMTP 配置 | — |

</details>

### 运行

```bash
python main.py
# 或
uvicorn main:app --host 0.0.0.0 --port 8010
```

- 服务地址：`http://localhost:8010`
- 交互式 API 文档（Swagger）：`http://localhost:8010/docs`

## 📡 API

所有 REST 接口需携带请求头 `X-API-Key: <你的网关Key>`。

### REST

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/session/create` | 创建会话，返回 `session_id` |
| `GET` | `/api/session/list` | 会话列表 |
| `GET` | `/api/skills/list` | 已加载技能及其参数 Schema |
| `POST` | `/api/skills/reload` | 热重载技能插件 |
| `GET` | `/api/chat/traffic` | 流量统计（QPS / 并发 / 熔断状态） |
| `GET` | `/api/chat/queue` | 任务队列统计（等待 / 运行 / 平均等待时长） |
| `GET` | `/api/chat/tokens?session_id=` | Token 用量（全局 / Top Key / Top 会话 / 按小时） |

### WebSocket 对话

```
ws://localhost:8010/api/chat/ws?session_id=<sid>&api_key=<key>
```

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

## 🧩 技能系统

技能 = `skills/defs/` 下的一个 Markdown 文件：**YAML front-matter 声明元信息，正文作为工具描述喂给 LLM**。修改后调用 `POST /api/skills/reload` 即可热加载。

```markdown
---
name: file_read
description: 读取沙箱内的文本文件
executor: file_read          # 对应 skill_executor 中的执行器
parameters:                  # OpenAI JSON Schema
  type: object
  properties:
    path:
      type: string
      description: 沙箱内相对路径
  required: [path]
---

补充给模型的工具使用说明，会拼接到工具描述中。
```

**内置技能**

| 技能 | 执行器 | 说明 |
|---|---|---|
| `bash` | bash | 沙箱内执行白名单 shell 命令（无管道/重定向/替换，10s 超时） |
| `file_list` / `file_read` / `file_write` | file | 沙箱内文件列表 / 读取 / 写入 |
| `send_email` | smtp | 发送邮件（aiosmtplib） |
| `design_travel_plan` | travel | 生成旅行攻略文档并落盘沙箱 |

## 🛡 安全与治理

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

## 📊 可观测性

- `logs/app.log.*` — 应用日志（JSON 行格式，按天滚动）
- `logs/audit.log.*` — 审计日志（用户消息、工具调用与结果、Token 消耗）
- `logs/tokens.jsonl` — 每次模型调用一行用量记录（Key 自动脱敏）
- 运行时指标：`/api/chat/traffic`、`/api/chat/queue`、`/api/chat/tokens`

## 📄 License

MIT


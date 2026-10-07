# MinaClaw 技能管理 API · 前端对接方案

技能列表展示、zip 包上传、技能删除三个接口的前端对接文档。所有接口需网关鉴权；**上传与删除成功后服务端自动热加载生效，前端无需额外调用 reload**。

- Base URL：`http://<host>:8010`（按部署环境替换）
- 鉴权方式：所有请求必须携带请求头 `X-API-Key: <网关Key>`
- 交互式调试：`http://<host>:8010/docs`（Swagger）

---

## 1. 通用约定

### 1.1 错误响应结构

业务错误统一返回 JSON，`detail` 为**人类可读的中文错误信息**（可直接透传给用户）：

```json
{ "detail": "技能 'demo' 已存在（可传 overwrite=true 覆盖）" }
```

> ⚠️ 例外：multipart 请求缺 `file` 字段时，FastAPI 返回 422，此时 `detail` 是**数组**（校验错误对象），前端展示前请判断类型。

### 1.2 全局错误码

| 状态码 | 含义 | 前端处理建议 |
|---|---|---|
| 401 | 无效 / 缺失 API Key | 跳转登录或提示配置 Key |
| 500 | 服务端内部错误 | 提示稍后重试并上报日志 |

各接口专属错误码见下文各节。

### 1.3 TypeScript 类型定义

```typescript
/** GET /api/skills/list 响应 */
interface SkillListResponse {
  progressive: boolean;      // 是否渐进式加载模式（load_skill 元工具）
  skills: SkillSummary[];
}

/** 单个技能摘要（列表项 / 上传成功返回结构一致） */
interface SkillSummary {
  name: string;              // 技能名，^[a-z0-9-]+$，即工具函数名
  description: string;       // 摘要（≤1024 字符，用于展示与 LLM 路由）
  executor: string;          // 执行器：file_read | file_write | file_list | bash | email | template | http
  parameters?: object;       // OpenAI JSON Schema（上传返回中无此字段）
  detail_chars?: number;     // 完整定义字符数（正文+附属文件内联，列表返回）
  source?: 'dir' | 'file';   // 目录形式（新标准）/ 单文件（旧格式兼容）
  assets?: string[];         // 附属文件相对路径（如 ["references/prefs.md"]）
  overwritten?: boolean;     // 仅上传返回：是否覆盖了同名旧技能
}

/** DELETE /api/skills/{name} 响应 */
interface SkillDeleteResponse {
  name: string;
  removed: string[];         // 实际删除的路径（目录 "xxx/" 或旧单文件 "xxx.md"）
}
```

---

## 2. 接口详情

### 2.1 展示技能列表

```
GET /api/skills/list
```

**请求示例**

```bash
curl -H "X-API-Key: dev-key-123" http://localhost:8010/api/skills/list
```

**成功响应 `200`**

```json
{
  "progressive": true,
  "skills": [
    {
      "name": "design-travel-plan",
      "description": "根据目的地、天数、偏好生成旅游攻略框架文档。当用户提到旅行计划、行程规划或需要产出攻略文档时使用。",
      "executor": "template",
      "parameters": { "type": "object", "properties": { "destination": { "type": "string" }, "days": { "type": "integer" } }, "required": ["destination", "days"] },
      "detail_chars": 856,
      "source": "dir",
      "assets": ["references/prefs.md"]
    }
  ]
}
```

字段展示建议：`name` + `description` 做主信息；`executor` 做标签徽章；`assets` 折叠展示；`detail_chars` 可换算为「详情约 N 字」提示（渐进式模式下 LLM 通过 `load_skill` 按需拉取完整定义，列表不包含正文）。

---

### 2.2 上传技能包（zip）

```
POST /api/skills/upload
Content-Type: multipart/form-data
```

**请求字段**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `file` | File | 是 | `.zip` 技能目录包，一次仅含**一个**技能 |
| `overwrite` | boolean (form) | 否 | 默认 `false`；`true` 时同名技能被完全替换（旧附属文件一并移除） |

**zip 包规范**（可据此做前端预检与用户提示）：两种布局均可——

- 布局 A（打包技能目录内容）：zip 根下直接是 `SKILL.md` + 附属文件
- 布局 B（打包技能目录本身）：zip 内为 `<skill-name>/SKILL.md`，目录名须与 front-matter 的 `name` 一致

约束：`name` 须匹配 `^[a-z0-9-]+$` 且 ≤64 字符；`description` ≤1024 字符；`executor` 必须是注册表内的执行器（`load_skill` 名与 `skill_loader` 执行器为系统保留）；附属文件扩展名白名单：md/txt/json/csv/yaml/yml/html/css/js/py/log；隐藏与系统条目（`.DS_Store`、`__MACOSX`）自动跳过；限额：压缩包 ≤5MB、解压后 ≤20MB、≤50 个文件（服务端可配，前端 5MB 预检仅作提示）。

**专属错误码**

| 状态码 | 场景 | `detail` 示例 |
|---|---|---|
| 400 | 非 zip 文件 / 缺 SKILL.md / 多个技能 / name 与目录名不一致 / name 不合规 / 未知执行器 / 含 `..` 等路径穿越条目 / 非白名单扩展名 | `文件不是有效的 zip 压缩包` |
| 409 | 同名技能已存在且未传 `overwrite=true` | `技能 'demo' 已存在（可传 overwrite=true 覆盖）` |
| 413 | 压缩包超 5MB / 解压超 20MB / 文件数超 50 | `压缩包超过 5MB 限制` |
| 422 | multipart 缺 `file` 字段（`detail` 为数组） | — |
| 503 | 上传功能被禁用（`SKILL_UPLOAD_ENABLED=false`） | `技能上传已禁用…` |

**请求示例**

```bash
curl -X POST http://localhost:8010/api/skills/upload \
  -H "X-API-Key: dev-key-123" \
  -F "file=@./my-skill.zip" \
  -F "overwrite=false"
```

**成功响应 `200`**（返回摘要，`assets` 含附属文件相对路径）

```json
{
  "name": "curl-upload-demo",
  "description": "服务级验证技能，通过 curl multipart 上传。",
  "executor": "template",
  "assets": ["references/tips.md"],
  "overwritten": false
}
```

成功后技能已**立即生效**（tools 已刷新），前端刷新列表即可看到。


---

### 2.3 删除技能

```
DELETE /api/skills/{name}
```

**路径参数**：`name` — 技能名（即列表中的 `name` 字段）

**专属错误码**

| 状态码 | 场景 | `detail` 示例 |
|---|---|---|
| 400 | 删除系统保留的 `load_skill` / 非法技能名 | `内置元工具 'load_skill' 不可删除` |
| 404 | 技能不存在 | `技能不存在: 'demo'` |

**请求示例**

```bash
curl -X DELETE http://localhost:8010/api/skills/curl-upload-demo \
  -H "X-API-Key: dev-key-123"
```

**成功响应 `200`**

```json
{ "name": "curl-upload-demo", "removed": ["curl-upload-demo/"] }
```

删除后立即生效，前端从列表中移除该项即可。内置技能同样可被删除（如需恢复将对应 zip 重新上传）。

---

### 2.4 附：手动热重载（一般无需调用）

```
POST /api/skills/reload
```

运维用途：直接改动服务器 `skills/defs/` 文件后手动刷新。上传 / 删除接口内部已自动执行同样操作。成功响应 `200`：`{"skills": ["bash", "design-travel-plan", ...]}`。

> 热重载会使注册表世代自增，各会话中旧版技能的加载记录随之失效（见第 5 节），下一轮按新版详情重新注入。

---

## 3. API Client 封装示例

### 3.1 fetch 版（原生，无依赖）

```typescript
const BASE_URL = 'http://localhost:8010';
const API_KEY = '<your-key>';

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE_URL}${path}`, {
    ...init,
    headers: { 'X-API-Key': API_KEY, ...init?.headers },
  });
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    // detail 可能是 string（业务错误）或数组（422 校验错误）
    const detail = body?.detail;
    const message = Array.isArray(detail)
      ? detail.map((d: any) => d.msg).join('; ')
      : detail ?? `请求失败（HTTP ${res.status}）`;
    throw new ApiError(message, res.status);
  }
  return body as T;
}

export class ApiError extends Error {
  constructor(message: string, public status: number) { super(message); }
}

export const skillsApi = {
  list: () => request<SkillListResponse>('/api/skills/list'),

  upload: (file: File, overwrite = false) => {
    const form = new FormData();
    form.append('file', file);            // 无需手动设置 Content-Type
    form.append('overwrite', String(overwrite));
    return request<SkillSummary & { overwritten: boolean }>(
      '/api/skills/upload', { method: 'POST', body: form });
  },

  remove: (name: string) =>
    request<SkillDeleteResponse>(`/api/skills/${encodeURIComponent(name)}`,
      { method: 'DELETE' }),
};
```

### 3.2 axios 版

```typescript
import axios from 'axios';

const http = axios.create({
  baseURL: 'http://localhost:8010',
  headers: { 'X-API-Key': '<your-key>' },
});

export const skillsApi = {
  list: () => http.get<SkillListResponse>('/api/skills/list'),

  upload: (file: File, overwrite = false) => {
    const form = new FormData();
    form.append('file', file);
    form.append('overwrite', String(overwrite));
    return http.post<SkillSummary & { overwritten: boolean }>(
      '/api/skills/upload', form);        // axios 自动设置 multipart 边界
  },

  remove: (name: string) =>
    http.delete<SkillDeleteResponse>(`/api/skills/${encodeURIComponent(name)}`),
};
```

---

## 4. 前端交互建议

**上传表单**
- 预检仅两项：扩展名 `.zip`、大小 ≤5MB（超限直接前端拦截并提示，避免白传）
- 收到 409 时弹出确认框「技能 xxx 已存在，是否覆盖？」，确认后带 `overwrite=true` 重传
- 400/413 的 `detail` 是服务端精确校验信息（含具体哪条不合规），可直接 toast 展示
- 上传期间禁用提交按钮；成功后刷新列表并高亮新技能

**列表展示**
- 操作列「删除」需二次确认（输入技能名或弹窗确认），删除失败 404 时静默刷新列表
- `source === 'file'` 的技能标记「旧格式」徽章，提示可迁移为目录形式后重新上传
- `progressive === true` 时可展示「渐进式加载」说明：列表注入的是摘要，完整定义由模型经 `load_skill` 按需获取，`detail_chars` 可作为体积参考

**状态同步**
- 上传 / 删除均即时生效，无需轮询；多客户端场景在操作后统一 `list()` 刷新
- 无需为上传成功追加 `reload` 调用（内部已自动热加载）

---

## 5. 附：会话级技能加载缓存行为说明（渐进式模式）

服务端在会话中记录已通过 `load_skill` 成功加载的技能（记录随会话持久化于 `sessions.meta`，重启后恢复），以消除跨轮重复加载。开关：`SKILL_SESSION_CACHE`（默认 `true`，仅渐进式模式生效）与 `SKILL_SESSION_CACHE_MAX_CHARS`（注入详情总字符上限，默认 `4000`）。

**轮首注入**：新轮组装上下文时，已加载且仍有效的技能完整详情会作为一条 `[已加载技能]` 系统消息注入（最近加载优先，计入上下文 token 预算）；超出字符上限的技能仅列名提示可重新 `load_skill` 获取。模型当轮可直接使用，无需再次调用 `load_skill`。

**命中去重**：详情已注入当前上下文时再次调用 `load_skill`，返回简短提示而非全文（`tool_end` 事件中可见，字段向后兼容，旧前端忽略即可）：

```json
{ "success": true, "cached": true, "skill": "pdf", "output": "该技能完整说明已在当前上下文中（系统注入），无需重复加载。" }
```

详情未注入当前上下文（超限列名 / 被裁剪 / 缓存关闭）时，`load_skill` 仍返回完整定义，并将该技能的加载最近性提前。

**失效行为**：技能上传（含覆盖）、删除、热重载都会使注册表世代自增——旧加载记录随之失效：失效技能不再注入，再次 `load_skill` 返回新版完整定义并按新世代重新记录；已删除技能的记录被清除，`load_skill` 返回未知技能错误。设置 `SKILL_SESSION_CACHE=false` 可完全关闭本能力（不注入、`load_skill` 恒返回全文，行为与未引入前一致）。

# Spec Delta

## Purpose

技能渐进式加载：LLM 请求的 tools 仅注入技能摘要，完整定义经 `load_skill` 元工具按需获取，并以会话级加载缓存避免跨轮重复加载。

## ADDED Requirements

### Requirement: 渐进式摘要注入
渐进式模式（`SKILL_PROGRESSIVE=true`）开启时，系统 SHALL 在每轮 LLM 请求的 tools 中仅注入各技能摘要（name + description + 参数 Schema）与 `load_skill` 元工具；渐进式模式关闭时 SHALL 全量注入技能完整定义且不注入 `load_skill` 元工具。

#### Scenario: 渐进式模式下的工具列表
- **WHEN** 渐进式模式开启且技能注册表非空
- **THEN** 注入的 tools 含全部技能摘要与 `load_skill` 元工具，且任一技能描述不含正文与附属文件内容

#### Scenario: 渐进式模式关闭时全量注入
- **WHEN** 渐进式模式关闭
- **THEN** 注入的 tools 含各技能完整定义，且不注入 `load_skill` 元工具

### Requirement: load_skill 返回完整定义
`load_skill` SHALL 返回目标技能的完整定义（description + 正文 + 附属文件内联，受 20000 字符截断上限约束）；未知技能名 SHALL 返回失败与可用技能名列表；空名或元工具自身名 SHALL 被拒绝。

#### Scenario: 首次加载技能
- **WHEN** 模型对已注册技能 `pdf` 首次调用 `load_skill`
- **THEN** 返回 `success: true` 与该技能完整定义文本

#### Scenario: 未知技能名
- **WHEN** 模型调用 `load_skill` 传入未注册技能名
- **THEN** 返回 `success: false`、错误信息与可用技能名列表

#### Scenario: 非法技能名
- **WHEN** 模型调用 `load_skill` 传入空字符串或 `load_skill` 自身
- **THEN** 返回 `success: false` 与「无效的技能名」错误

### Requirement: 会话级加载记录
`load_skill` 成功返回完整定义后，系统 SHALL 在会话中记录该技能（技能名与加载时的注册表世代），并持久化到会话元数据，进程重启后记录 SHALL 可恢复；记录按加载最近性排序。

#### Scenario: 加载后写入会话记录
- **WHEN** 会话中 `load_skill` 对技能 `pdf` 成功返回全文
- **THEN** 该会话的加载记录含 `pdf` 与当前注册表世代，且已写入会话元数据

#### Scenario: 重启后记录恢复
- **WHEN** 服务重启后该会话再次组装上下文
- **THEN** 加载记录自会话元数据恢复，已加载且仍有效的技能继续参与轮首注入

### Requirement: 轮首详情注入
会话加载缓存开启且渐进式模式生效时，系统 SHALL 在新轮组装上下文时把已加载且仍有效的技能完整详情作为一条系统消息注入（最近加载优先），注入 SHALL 受字符上限与上下文 token 预算双重约束；超出上限的技能仅列名提示可重新获取；未加载或已失效的技能 SHALL NOT 注入；缓存开关关闭时 SHALL NOT 注入。

#### Scenario: 新轮注入已加载技能
- **WHEN** 会话上一轮已加载技能 `pdf` 且未被热重载失效，用户新一轮发送消息
- **THEN** 组装的上下文中含一条系统消息，内嵌 `pdf` 完整详情并注明无需再调用 `load_skill`

#### Scenario: 超出字符上限
- **WHEN** 已加载技能详情总量超过注入字符上限
- **THEN** 最近加载的技能详情优先注入，其余技能仅以名称列出并提示可重新 `load_skill` 获取

#### Scenario: 缓存开关关闭
- **WHEN** `SKILL_SESSION_CACHE=false`
- **THEN** 新轮不注入已加载技能详情，上下文组装行为与未引入本能力前一致

### Requirement: 命中去重
`load_skill` 命中「本会话已记录且详情已注入当前上下文」时 SHALL 返回简短提示（`success: true` 且 `cached: true`）而非全文；详情未注入当前上下文（超限未注入、被裁剪或缓存关闭）时 SHALL 返回完整定义，并将该技能的加载最近性提前。

#### Scenario: 命中缓存返回简短提示
- **WHEN** 技能 `pdf` 详情已注入当前轮上下文，模型再次调用 `load_skill`
- **THEN** 返回 `success: true`、`cached: true` 与简短提示文本，且不含技能全文

#### Scenario: 详情不在上下文时返回全文
- **WHEN** 技能 `pdf` 已记录但详情因超出注入上限未进入当前上下文
- **THEN** `load_skill` 返回完整定义，且该技能在加载记录中的排序被提前

### Requirement: 热重载失效
注册表世代变化（技能上传、删除、热重载）后，系统 SHALL 使会话中受影响的旧加载记录失效：失效技能不再注入；再次 `load_skill` SHALL 返回新版完整定义并按新世代重新记录；已删除技能的记录 SHALL 被清除。

#### Scenario: 技能重新上传后失效
- **WHEN** 技能 `pdf` 被覆盖上传并热重载，随后同一会话中再次调用 `load_skill`
- **THEN** 返回新版完整定义，会话记录更新为新世代，下一轮按新版本注入

#### Scenario: 技能删除后清除记录
- **WHEN** 已加载技能 `pdf` 被删除并热重载
- **THEN** 会话记录中 `pdf` 被清除，后续轮次不再注入，`load_skill("pdf")` 返回未知技能错误

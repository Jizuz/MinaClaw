-- =============================================================
-- Claw Agent Gateway - PostgreSQL Schema
-- 版本: 2.0
-- 说明: AI Agent 网关的完整数据模型
-- 字符集: UTF-8
-- 时区: UTC（应用层负责转换）
-- =============================================================

-- -------------------------------------------------------------
-- 扩展
-- -------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";     -- UUID 生成（备用）
-- CREATE EXTENSION IF NOT EXISTS "pg_trgm";     -- 模糊搜索（可选）
-- CREATE EXTENSION IF NOT EXISTS "vector";      -- 向量搜索（RAG 时启用）


-- =============================================================
-- 1. 用户表
-- =============================================================
CREATE TABLE IF NOT EXISTS users (
    id              VARCHAR(32)     NOT NULL,
    username        VARCHAR(64)     NOT NULL,
    password_hash   VARCHAR(128),
    display_name    VARCHAR(128),
    email           VARCHAR(255),
    status          VARCHAR(16)     NOT NULL DEFAULT 'active',
    tier            VARCHAR(16)     NOT NULL DEFAULT 'free',
    meta            JSONB,
    created_at      TIMESTAMP(3)    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP(3)    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_login_at   TIMESTAMP(3),

    CONSTRAINT pk_users PRIMARY KEY (id),
    CONSTRAINT uk_users_username UNIQUE (username),
    CONSTRAINT uk_users_email UNIQUE (email),
    CONSTRAINT ck_users_status CHECK (status IN ('active', 'suspended', 'deleted')),
    CONSTRAINT ck_users_tier   CHECK (tier   IN ('free', 'pro', 'enterprise'))
);

COMMENT ON TABLE  users                 IS '用户表：系统所有账号的根实体';
COMMENT ON COLUMN users.id              IS '用户 ID，12 位 hex 字符串（如 3f8a2c1d9e04）';
COMMENT ON COLUMN users.username        IS '用户名，全局唯一，用于登录';
COMMENT ON COLUMN users.password_hash   IS '密码哈希（bcrypt），纯 API Key 用户为 NULL';
COMMENT ON COLUMN users.display_name    IS '展示名，UI 上显示用';
COMMENT ON COLUMN users.email           IS '邮箱，可选，用于通知与找回密码';
COMMENT ON COLUMN users.status          IS '账号状态：active=正常 / suspended=停用 / deleted=已删';
COMMENT ON COLUMN users.tier            IS '用户等级：free=免费 / pro=付费 / enterprise=企业，决定配额';
COMMENT ON COLUMN users.meta            IS '扩展元信息（JSONB），如偏好设置、来源渠道';
COMMENT ON COLUMN users.created_at      IS '创建时间（UTC）';
COMMENT ON COLUMN users.updated_at      IS '更新时间（UTC），由触发器自动维护';
COMMENT ON COLUMN users.last_login_at   IS '最后登录时间（UTC），用于活跃度统计';

CREATE INDEX IF NOT EXISTS idx_users_status      ON users(status);
CREATE INDEX IF NOT EXISTS idx_users_tier        ON users(tier);
CREATE INDEX IF NOT EXISTS idx_users_created_at  ON users(created_at DESC);


-- =============================================================
-- 2. API Key 表
-- =============================================================
CREATE TABLE IF NOT EXISTS api_keys (
    id              VARCHAR(32)     NOT NULL,
    key_hash        VARCHAR(128)    NOT NULL,
    key_prefix      VARCHAR(12)     NOT NULL,
    user_id         VARCHAR(32)     NOT NULL,
    name            VARCHAR(64),
    scopes          JSONB           NOT NULL DEFAULT '["chat"]'::jsonb,
    revoked         BOOLEAN         NOT NULL DEFAULT FALSE,
    expires_at      TIMESTAMP(3),
    last_used_at    TIMESTAMP(3),
    created_at      TIMESTAMP(3)    NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT pk_api_keys PRIMARY KEY (id),
    CONSTRAINT uk_api_keys_hash UNIQUE (key_hash),
    CONSTRAINT fk_api_keys_user FOREIGN KEY (user_id)
        REFERENCES users(id) ON DELETE CASCADE
);

COMMENT ON TABLE  api_keys              IS 'API Key 表：服务端/CLI 调用的凭据';
COMMENT ON COLUMN api_keys.id           IS 'API Key 内部 ID';
COMMENT ON COLUMN api_keys.key_hash     IS 'Key 的 sha256 哈希值（不存明文）';
COMMENT ON COLUMN api_keys.key_prefix   IS 'Key 前缀（前 12 位），仅用于展示';
COMMENT ON COLUMN api_keys.user_id      IS '归属用户 ID';
COMMENT ON COLUMN api_keys.name         IS 'Key 名称，用户自定义，便于管理';
COMMENT ON COLUMN api_keys.scopes       IS '权限范围（JSONB 数组），如 ["chat","files"]';
COMMENT ON COLUMN api_keys.revoked      IS '是否已吊销，TRUE 后立即失效';
COMMENT ON COLUMN api_keys.expires_at   IS '过期时间，NULL 表示永不过期';
COMMENT ON COLUMN api_keys.last_used_at IS '最后使用时间，用于审计与清理';
COMMENT ON COLUMN api_keys.created_at   IS '创建时间（UTC）';

CREATE INDEX IF NOT EXISTS idx_api_keys_user    ON api_keys(user_id);
CREATE INDEX IF NOT EXISTS idx_api_keys_prefix  ON api_keys(key_prefix);
CREATE INDEX IF NOT EXISTS idx_api_keys_scopes  ON api_keys USING GIN (scopes);


-- =============================================================
-- 3. 会话表
-- =============================================================
CREATE TABLE IF NOT EXISTS sessions (
    id              VARCHAR(32)     NOT NULL,
    user_id         VARCHAR(32)     NOT NULL,
    title           VARCHAR(255),
    summary         TEXT,
    meta            JSONB,
    archived        BOOLEAN         NOT NULL DEFAULT FALSE,
    message_count   INTEGER         NOT NULL DEFAULT 0,
    total_tokens    BIGINT          NOT NULL DEFAULT 0,
    created_at      TIMESTAMP(3)    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP(3)    NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT pk_sessions PRIMARY KEY (id),
    CONSTRAINT fk_sessions_user FOREIGN KEY (user_id)
        REFERENCES users(id) ON DELETE CASCADE
);

COMMENT ON TABLE  sessions                IS '会话表：一次对话的容器，归属某个用户';
COMMENT ON COLUMN sessions.id             IS '会话 ID，12 位 hex 字符串';
COMMENT ON COLUMN sessions.user_id        IS '归属用户 ID，用于越权防护';
COMMENT ON COLUMN sessions.title          IS '会话标题，可自动生成或用户命名';
COMMENT ON COLUMN sessions.summary        IS '上下文压缩后的摘要文本';
COMMENT ON COLUMN sessions.meta           IS '会话元信息（JSONB），如客户端类型、标签';
COMMENT ON COLUMN sessions.archived       IS '是否已归档，归档后不再出现在列表';
COMMENT ON COLUMN sessions.message_count  IS '消息数冗余计数，避免实时 COUNT';
COMMENT ON COLUMN sessions.total_tokens   IS '会话累计消耗 Token 数，用于统计';
COMMENT ON COLUMN sessions.created_at     IS '创建时间（UTC）';
COMMENT ON COLUMN sessions.updated_at     IS '更新时间（UTC），由触发器自动维护';

CREATE INDEX IF NOT EXISTS idx_sessions_user_updated
    ON sessions(user_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_updated
    ON sessions(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_archived
    ON sessions(archived) WHERE archived = FALSE;


-- =============================================================
-- 4. 消息表
-- =============================================================
CREATE TABLE IF NOT EXISTS messages (
    id              BIGINT          GENERATED ALWAYS AS IDENTITY,
    session_id      VARCHAR(32)     NOT NULL,
    role            VARCHAR(16)     NOT NULL,
    content         TEXT            NOT NULL,
    tokens          INTEGER         NOT NULL DEFAULT 0,
    tool_calls      JSONB,
    tool_call_id    VARCHAR(64),
    parent_id       BIGINT,
    meta            JSONB,
    created_at      TIMESTAMP(3)    NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT pk_messages PRIMARY KEY (id),
    CONSTRAINT fk_messages_session FOREIGN KEY (session_id)
        REFERENCES sessions(id) ON DELETE CASCADE,
    CONSTRAINT ck_messages_role CHECK (
        role IN ('system', 'user', 'assistant', 'tool')
    )
);

COMMENT ON TABLE  messages               IS '消息表：会话中的每一条消息';
COMMENT ON COLUMN messages.id            IS '消息 ID，自增主键，保证同一会话内有序';
COMMENT ON COLUMN messages.session_id    IS '所属会话 ID';
COMMENT ON COLUMN messages.role          IS '角色：system/user/assistant/tool';
COMMENT ON COLUMN messages.content       IS '消息内容，工具调用时可能为空字符串';
COMMENT ON COLUMN messages.tokens        IS '该消息消耗的 Token 数（预估或实测）';
COMMENT ON COLUMN messages.tool_calls    IS '工具调用请求（JSONB），仅 assistant 消息有';
COMMENT ON COLUMN messages.tool_call_id  IS '工具调用 ID，用于 assistant ↔ tool 消息关联';
COMMENT ON COLUMN messages.parent_id     IS '父消息 ID，用于消息编辑/重新生成追踪';
COMMENT ON COLUMN messages.meta          IS '扩展元信息（JSONB），如模型名、延迟';
COMMENT ON COLUMN messages.created_at    IS '创建时间（UTC），同会话内保证单调递增';

CREATE INDEX IF NOT EXISTS idx_messages_session
    ON messages(session_id, id);
CREATE INDEX IF NOT EXISTS idx_messages_created
    ON messages(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_role
    ON messages(session_id, role);
CREATE INDEX IF NOT EXISTS idx_messages_tool_calls
    ON messages USING GIN (tool_calls);


-- =============================================================
-- 5. 工具调用审计表
-- =============================================================
CREATE TABLE IF NOT EXISTS tool_calls (
    id              BIGINT          GENERATED ALWAYS AS IDENTITY,
    session_id      VARCHAR(32)     NOT NULL,
    user_id         VARCHAR(32)     NOT NULL,
    message_id      BIGINT,
    tool_name       VARCHAR(64)     NOT NULL,
    arguments       JSONB,
    result          JSONB,
    success         BOOLEAN         NOT NULL DEFAULT TRUE,
    error_message   TEXT,
    duration_ms     INTEGER,
    created_at      TIMESTAMP(3)    NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT pk_tool_calls PRIMARY KEY (id),
    CONSTRAINT fk_tool_calls_session FOREIGN KEY (session_id)
        REFERENCES sessions(id) ON DELETE CASCADE
);

COMMENT ON TABLE  tool_calls                IS '工具调用审计表：记录每次工具执行的完整信息';
COMMENT ON COLUMN tool_calls.id             IS '记录 ID，自增主键';
COMMENT ON COLUMN tool_calls.session_id     IS '所属会话 ID';
COMMENT ON COLUMN tool_calls.user_id        IS '调用用户 ID（冗余，便于按用户查询）';
COMMENT ON COLUMN tool_calls.message_id     IS '关联的 assistant 消息 ID';
COMMENT ON COLUMN tool_calls.tool_name      IS '工具名称，如 file_read / bash / send_email';
COMMENT ON COLUMN tool_calls.arguments      IS '调用参数（JSONB）';
COMMENT ON COLUMN tool_calls.result         IS '执行结果（JSONB），含 success/output/error';
COMMENT ON COLUMN tool_calls.success        IS '是否成功执行';
COMMENT ON COLUMN tool_calls.error_message  IS '失败时的错误信息（冗余，便于查询）';
COMMENT ON COLUMN tool_calls.duration_ms    IS '执行耗时（毫秒）';
COMMENT ON COLUMN tool_calls.created_at     IS '调用时间（UTC）';

CREATE INDEX IF NOT EXISTS idx_tool_calls_session
    ON tool_calls(session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tool_calls_user
    ON tool_calls(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tool_calls_tool
    ON tool_calls(tool_name, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tool_calls_success
    ON tool_calls(success) WHERE success = FALSE;
CREATE INDEX IF NOT EXISTS idx_tool_calls_arguments
    ON tool_calls USING GIN (arguments);


-- =============================================================
-- 6. Token 用量表
-- =============================================================
CREATE TABLE IF NOT EXISTS token_usages (
    id                  BIGINT          GENERATED ALWAYS AS IDENTITY,
    user_id             VARCHAR(32)     NOT NULL,
    day                 DATE            NOT NULL,
    model               VARCHAR(64)     NOT NULL DEFAULT 'default',
    prompt_tokens       BIGINT          NOT NULL DEFAULT 0,
    completion_tokens   BIGINT          NOT NULL DEFAULT 0,
    total_tokens        BIGINT          NOT NULL DEFAULT 0,
    request_count       BIGINT          NOT NULL DEFAULT 0,
    tool_call_count     BIGINT          NOT NULL DEFAULT 0,
    error_count         BIGINT          NOT NULL DEFAULT 0,
    updated_at          TIMESTAMP(3)    NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT pk_token_usages PRIMARY KEY (id),
    CONSTRAINT uk_token_usages_user_day_model UNIQUE (user_id, day, model),
    CONSTRAINT ck_token_usages_day CHECK (day <= CURRENT_DATE)
);

COMMENT ON TABLE  token_usages                    IS 'Token 用量表：按用户/日/模型聚合的消耗统计';
COMMENT ON COLUMN token_usages.id                 IS '记录 ID，自增主键';
COMMENT ON COLUMN token_usages.user_id            IS '用户 ID';
COMMENT ON COLUMN token_usages.day                IS '统计日期（UTC），按日聚合';
COMMENT ON COLUMN token_usages.model              IS '模型名，如 glm-4-plus / glm-4-flash';
COMMENT ON COLUMN token_usages.prompt_tokens      IS '当日输入 Token 总数';
COMMENT ON COLUMN token_usages.completion_tokens  IS '当日输出 Token 总数';
COMMENT ON COLUMN token_usages.total_tokens       IS '当日总 Token 数（= prompt + completion）';
COMMENT ON COLUMN token_usages.request_count      IS '当日请求次数';
COMMENT ON COLUMN token_usages.tool_call_count    IS '当日工具调用次数';
COMMENT ON COLUMN token_usages.error_count        IS '当日错误次数';
COMMENT ON COLUMN token_usages.updated_at         IS '最后更新时间（UTC），由触发器自动维护';

CREATE INDEX IF NOT EXISTS idx_token_usages_user_day
    ON token_usages(user_id, day DESC);
CREATE INDEX IF NOT EXISTS idx_token_usages_day
    ON token_usages(day DESC);
CREATE INDEX IF NOT EXISTS idx_token_usages_model
    ON token_usages(model, day DESC);


-- =============================================================
-- 7. 触发器：自动维护 updated_at
-- =============================================================
CREATE OR REPLACE FUNCTION trg_set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION trg_set_updated_at() IS '通用触发器函数：UPDATE 时自动更新 updated_at';

-- users
DROP TRIGGER IF EXISTS set_updated_at_users ON users;
CREATE TRIGGER set_updated_at_users
    BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION trg_set_updated_at();

-- sessions
DROP TRIGGER IF EXISTS set_updated_at_sessions ON sessions;
CREATE TRIGGER set_updated_at_sessions
    BEFORE UPDATE ON sessions
    FOR EACH ROW EXECUTE FUNCTION trg_set_updated_at();

-- token_usages
DROP TRIGGER IF EXISTS set_updated_at_token_usages ON token_usages;
CREATE TRIGGER set_updated_at_token_usages
    BEFORE UPDATE ON token_usages
    FOR EACH ROW EXECUTE FUNCTION trg_set_updated_at();


-- =============================================================
-- 8. 视图：用户当日用量汇总
-- =============================================================
CREATE OR REPLACE VIEW v_user_daily_usage AS
SELECT
    u.id            AS user_id,
    u.username,
    u.tier,
    COALESCE(SUM(t.prompt_tokens),     0) AS prompt_tokens,
    COALESCE(SUM(t.completion_tokens), 0) AS completion_tokens,
    COALESCE(SUM(t.total_tokens),      0) AS total_tokens,
    COALESCE(SUM(t.request_count),     0) AS request_count,
    COALESCE(SUM(t.tool_call_count),   0) AS tool_call_count
FROM users u
LEFT JOIN token_usages t
    ON t.user_id = u.id AND t.day = CURRENT_DATE
GROUP BY u.id, u.username, u.tier;

COMMENT ON VIEW v_user_daily_usage IS '视图：每个用户当日的 Token 用量汇总';


-- =============================================================
-- 9. 视图：会话统计
-- =============================================================
CREATE OR REPLACE VIEW v_session_stats AS
SELECT
    s.id            AS session_id,
    s.user_id,
    s.title,
    s.archived,
    s.created_at,
    s.updated_at,
    COUNT(m.id)     AS actual_message_count,
    COALESCE(SUM(m.tokens), 0) AS actual_total_tokens
FROM sessions s
LEFT JOIN messages m ON m.session_id = s.id
GROUP BY s.id, s.user_id, s.title, s.archived, s.created_at, s.updated_at;

COMMENT ON VIEW v_session_stats IS '视图：会话的真实消息数与 Token 数（对账用）';
import os
from pathlib import Path
from pydantic_settings import BaseSettings

BASE_DIR = Path(__file__).parent
SANDBOX_DIR = BASE_DIR / "sandbox"
SANDBOX_DIR.mkdir(exist_ok=True)

LOG_DIR = Path(os.getenv("LOG_DIR", BASE_DIR / "logs"))
LOG_DIR.mkdir(exist_ok=True)


class Settings(BaseSettings):
    # OpenAI 兼容端点
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    openai_base_url: str = os.getenv(
        "OPENAI_BASE_URL", "https://open.bigmodel.cn/api/paas/v4"
    )
    openai_model: str = os.getenv("OPENAI_MODEL", "glm-4-plus")

    # 鉴权
    minaclaw_api_keys: str = os.getenv("MINACLAW_API_KEYS", "dev-key-123")

    # PostgreSQL（支持 jdbc:postgresql:// 与 postgresql:// 两种格式）
    database_url: str = os.getenv(
        "DATABASE_URL", "jdbc:postgresql://localhost:5432/mina")

    # JWT（登录注册）
    jwt_secret: str = os.getenv("JWT_SECRET", "change-me-in-production")
    jwt_expire_minutes: int = int(os.getenv("JWT_EXPIRE_MINUTES", "1440"))

    # 沙箱
    sandbox_dir: str = str(SANDBOX_DIR)
    max_file_size_mb: int = 10

    # Bash
    bash_timeout: int = 10

    # 会话
    max_context_tokens: int = 6000
    max_iterations: int = 8

    # 技能渐进式加载（tools 仅注入摘要，详情经 load_skill 按需获取）
    skill_progressive: bool = (
        os.getenv("SKILL_PROGRESSIVE", "true").lower() == "true"
    )

    # 技能上传（zip 目录包，对齐 Agent Skills 开放标准）
    skill_upload_enabled: bool = (
        os.getenv("SKILL_UPLOAD_ENABLED", "true").lower() == "true"
    )
    skill_upload_max_zip_mb: int = int(os.getenv("SKILL_UPLOAD_MAX_ZIP_MB", "5"))
    skill_upload_max_total_mb: int = int(
        os.getenv("SKILL_UPLOAD_MAX_TOTAL_MB", "20"))
    skill_upload_max_files: int = int(os.getenv("SKILL_UPLOAD_MAX_FILES", "50"))

    # 会话级技能加载缓存（轮首注入 + load_skill 命中去重，仅渐进式模式生效）
    skill_session_cache: bool = (
        os.getenv("SKILL_SESSION_CACHE", "true").lower() == "true"
    )
    skill_session_cache_max_chars: int = int(
        os.getenv("SKILL_SESSION_CACHE_MAX_CHARS", "4000"))

    # 日志
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    log_dir: str = str(LOG_DIR)
    log_json: bool = os.getenv("LOG_JSON", "true").lower() == "true"

    # Token 预算
    daily_token_quota: int = int(os.getenv("DAILY_TOKEN_QUOTA", "1000000"))
    daily_token_warn: int = int(os.getenv("DAILY_TOKEN_WARN", "800000"))

    # SMTP
    smtp_host: str = os.getenv("SMTP_HOST", "")
    smtp_port: int = int(os.getenv("SMTP_PORT", "587") or "587")
    smtp_user: str = os.getenv("SMTP_USER", "")
    smtp_pass: str = os.getenv("SMTP_PASS", "")
    smtp_from: str = os.getenv("SMTP_FROM", "claw@example.com")

    class Config:
        env_file = ".env"


settings = Settings()
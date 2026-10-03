"""
统一鉴权中间件

三种凭证方式（优先级从高到低）：
1. JWT          — Authorization: Bearer <token>（登录注册获得，users 表）
2. 数据库 API Key — X-API-Key: mc_xxx（api_keys 表，sha256 匹配，未吊销未过期）
3. 静态 Key     — MINACLAW_API_KEYS 环境变量（向后兼容，无归属用户）

HTTP 依赖 verify_api_key 双模式；WebSocket 用 authenticate(token=..., api_key=...)。
"""
import hashlib
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from fastapi import Header, HTTPException
from jose import JWTError, jwt

from config import settings
from db import pool as db
from utils.logger import get_logger, log_extra

log = get_logger("auth")


def get_valid_keys() -> set[str]:
    """静态 Key 白名单（向后兼容 dev-key-123；置空环境变量可强制全走数据库）"""
    return {k.strip() for k in settings.minaclaw_api_keys.split(",") if k.strip()}


def hash_key(raw: str) -> str:
    """API Key 的 sha256 哈希（入库 key_hash，不存明文）"""
    return hashlib.sha256(raw.encode()).hexdigest()


def utcnow_naive() -> datetime:
    """无时区 UTC 时间（与 DDL TIMESTAMP 列一致）"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass
class Principal:
    """统一身份：路由依赖与 WebSocket 共用"""
    user_id: str
    username: str
    tier: str
    via: str                     # jwt / db-key / static-key
    key: str                     # token_meter / 限流的稳定计量标识
    scopes: list = field(default_factory=list)

    @property
    def is_static(self) -> bool:
        return self.via == "static-key"


# ==================== JWT ====================

def create_access_token(user_id: str, username: str) -> str:
    now = int(time.time())
    payload = {
        "sub": user_id,
        "username": username,
        "iat": now,
        "exp": now + settings.jwt_expire_minutes * 60,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def _decode_token(token: str) -> dict:
    return jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])


# ==================== 统一认证入口 ====================

async def authenticate(token: str = "", api_key: str = "") -> Principal:
    """JWT 与 API Key 二选一；均无效时抛 401"""
    if not token and not api_key:
        raise HTTPException(status_code=401, detail="缺少认证凭证（Authorization 或 X-API-Key）")

    # ---- 1) JWT ----
    if token:
        try:
            payload = _decode_token(token)
        except JWTError as e:
            raise HTTPException(status_code=401, detail=f"无效或过期的登录凭证: {e}")
        user = await db.fetch_one(
            "SELECT id, username, status, tier FROM users WHERE id = %s",
            (payload.get("sub") or "",))
        if not user or user["status"] != "active":
            raise HTTPException(status_code=401, detail="账号不存在或已停用")
        return Principal(
            user_id=user["id"], username=user["username"], tier=user["tier"],
            via="jwt", key=f"jwt:{user['id']}", scopes=["chat", "keys"])

    # ---- 2) 数据库 API Key ----
    row = await db.fetch_one(
        "SELECT k.id AS key_id, k.scopes, k.revoked, k.expires_at, "
        "       u.id AS user_id, u.username, u.status, u.tier "
        "FROM api_keys k JOIN users u ON u.id = k.user_id "
        "WHERE k.key_hash = %s",
        (hash_key(api_key),))
    if row and not row["revoked"] and row["status"] == "active":
        expired = row["expires_at"] is not None and row["expires_at"] <= utcnow_naive()
        if not expired:
            # 尽力更新 last_used_at，失败不影响主流程
            try:
                await db.execute(
                    "UPDATE api_keys SET last_used_at = CURRENT_TIMESTAMP "
                    "WHERE id = %s", (row["key_id"],))
            except Exception as e:
                log.warning("update last_used_at failed",
                            extra=log_extra(error=str(e)))
            return Principal(
                user_id=row["user_id"], username=row["username"],
                tier=row["tier"], via="db-key",
                key=api_key, scopes=row["scopes"] or ["chat"])

    # ---- 3) 静态 Key（向后兼容） ----
    if api_key in get_valid_keys():
        return Principal(
            user_id="static", username="static", tier="free",
            via="static-key", key=f"static:{api_key[:6]}", scopes=["chat"])

    raise HTTPException(status_code=401, detail="Invalid API Key")


async def verify_api_key(
    authorization: str = Header(default=""),
    x_api_key: str = Header(default="", alias="X-API-Key"),
) -> Principal:
    """HTTP 路由依赖：Bearer JWT 或 X-API-Key 任一即可"""
    token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
    return await authenticate(token=token, api_key=x_api_key.strip())

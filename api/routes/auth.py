"""
认证路由：注册 / 登录 / 当前用户

- 密码 bcrypt 哈希入库（users.password_hash）
- 登录成功签发 JWT（HS256，过期时间 settings.jwt_expire_minutes）
- user id 为 12 位 hex（对齐 DDL 注释）
"""
import re
import secrets

import bcrypt
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from config import settings
from db import pool as db
from middleware.auth import (Principal, create_access_token, utcnow_naive,
                             verify_api_key)
from utils.logger import get_logger, log_extra

log = get_logger("auth.api")

router = APIRouter(prefix="/api/auth", tags=["认证鉴权"])

# bcrypt 算法历史限制：仅取前 72 字节（passlib 与旧版 bcrypt 同样截断）
_BCRYPT_MAX_BYTES = 72


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8")[:_BCRYPT_MAX_BYTES],
                         bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8")[:_BCRYPT_MAX_BYTES],
                              hashed.encode("ascii"))
    except ValueError:
        return False

USERNAME_RE = re.compile(r"^[a-zA-Z0-9_-]{3,64}$")


class RegisterRequest(BaseModel):
    username: str
    password: str
    display_name: str | None = None
    email: str | None = None


class LoginRequest(BaseModel):
    username: str
    password: str


def _iso(dt) -> str | None:
    return dt.isoformat() + "Z" if dt is not None else None


def _user_public(u: dict) -> dict:
    return {
        "id": u["id"],
        "username": u["username"],
        "display_name": u["display_name"],
        "email": u["email"],
        "tier": u["tier"],
        "status": u["status"],
        "created_at": _iso(u["created_at"]),
        "last_login_at": _iso(u.get("last_login_at")),
    }


@router.post("/register", status_code=201)
async def register(req: RegisterRequest):
    """注册新用户（用户名全局唯一，tier=free）"""
    if not USERNAME_RE.match(req.username or ""):
        raise HTTPException(400, "用户名须为 3-64 位字母/数字/下划线/连字符")
    if len(req.password or "") < 6:
        raise HTTPException(400, "密码至少 6 位")
    if req.email and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", req.email):
        raise HTTPException(400, "邮箱格式不正确")

    exists = await db.fetch_one(
        "SELECT id FROM users WHERE username = %s", (req.username,))
    if exists:
        raise HTTPException(409, f"用户名 '{req.username}' 已被占用")
    if req.email:
        exists = await db.fetch_one(
            "SELECT id FROM users WHERE email = %s", (req.email,))
        if exists:
            raise HTTPException(409, f"邮箱 '{req.email}' 已被注册")

    uid = secrets.token_hex(6)  # 12 位 hex，对齐 DDL 注释
    try:
        await db.execute(
            "INSERT INTO users (id, username, password_hash, display_name, email) "
            "VALUES (%s, %s, %s, %s, %s)",
            (uid, req.username, hash_password(req.password),
             req.display_name or req.username, req.email))
    except Exception as e:
        # 并发注册同名兜底
        if "uk_users_username" in str(e) or "uk_users_email" in str(e):
            raise HTTPException(409, "用户名或邮箱已被占用")
        raise

    log.info("user registered", extra=log_extra(user_id=uid, username=req.username))
    user = await db.fetch_one(
        "SELECT * FROM users WHERE id = %s", (uid,))
    return {"user": _user_public(user)}


@router.post("/login")
async def login(req: LoginRequest):
    """登录：校验密码 → 更新 last_login_at → 签发 JWT"""
    user = await db.fetch_one(
        "SELECT * FROM users WHERE username = %s", (req.username,))
    if not user or not user["password_hash"] or \
            not verify_password(req.password, user["password_hash"]):
        raise HTTPException(401, "用户名或密码错误")
    if user["status"] != "active":
        raise HTTPException(403, f"账号已停用（status={user['status']}）")

    await db.execute(
        "UPDATE users SET last_login_at = %s WHERE id = %s",
        (utcnow_naive(), user["id"]))

    token = create_access_token(user["id"], user["username"])
    log.info("user login", extra=log_extra(user_id=user["id"]))
    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_in": settings.jwt_expire_minutes * 60,
        "user": _user_public(user),
    }


@router.get("/me")
async def me(principal: Principal = Depends(verify_api_key)):
    """当前用户信息（JWT 或数据库 API Key 均可；静态 Key 无归属用户）"""
    if principal.is_static:
        raise HTTPException(401, "静态 Key 无归属用户，请先登录")
    user = await db.fetch_one("SELECT * FROM users WHERE id = %s",
                              (principal.user_id,))
    if not user:
        raise HTTPException(401, "账号不存在")
    return {"user": _user_public(user)}

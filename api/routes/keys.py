"""
API Key 管理路由（需登录身份，静态 Key 无归属用户不可操作）

- POST   /api/keys        创建（明文 Key 仅创建响应返回一次，库中只存 sha256）
- GET    /api/keys        列出当前用户全部 Key（不含哈希与明文）
- DELETE /api/keys/{id}   吊销（revoked=TRUE，立即失效，不可恢复）
"""
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from db import pool as db
from middleware.auth import (Principal, hash_key, utcnow_naive,
                             verify_api_key)
from utils.logger import get_logger, log_extra

log = get_logger("keys.api")

router = APIRouter(prefix="/api/keys", tags=["API Key 管理"])

ALLOWED_SCOPES = {"chat", "files"}

MAX_KEYS_PER_USER = 20


class KeyCreateRequest(BaseModel):
    name: str | None = None
    scopes: list[str] | None = None
    expires_at: datetime | None = None  # ISO 8601，可空表示永不过期


def _iso(dt) -> str | None:
    return dt.isoformat() if dt is not None else None


def _key_public(row: dict) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "key_prefix": row["key_prefix"],
        "scopes": row["scopes"] or [],
        "revoked": row["revoked"],
        "expires_at": _iso(row["expires_at"]),
        "last_used_at": _iso(row["last_used_at"]),
        "created_at": _iso(row["created_at"]),
    }


def _naive_utc(dt: datetime) -> datetime:
    """带时区时间统一转无时区 UTC（与 DDL TIMESTAMP 列一致）"""
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


@router.post("")
async def create_key(req: KeyCreateRequest,
                     p: Principal = Depends(verify_api_key)):
    """创建 API Key：mc_ + 48 位 hex 明文，sha256 入库"""
    if p.is_static:
        raise HTTPException(401, "静态 Key 无归属用户，请先登录后再创建")

    scopes = req.scopes or ["chat"]
    for s in scopes:
        if s not in ALLOWED_SCOPES:
            raise HTTPException(400, f"非法 scope: '{s}'（可选 {sorted(ALLOWED_SCOPES)}）")

    count = await db.fetch_one(
        "SELECT COUNT(*) AS n FROM api_keys WHERE user_id = %s", (p.user_id,))
    if count and count["n"] >= MAX_KEYS_PER_USER:
        raise HTTPException(409, f"每个用户最多 {MAX_KEYS_PER_USER} 个 API Key")

    raw = "mc_" + secrets.token_hex(24)          # 明文，仅本次响应返回
    kid = secrets.token_hex(6)
    expires = _naive_utc(req.expires_at) if req.expires_at else None
    if expires and expires <= utcnow_naive():
        raise HTTPException(400, "过期时间必须晚于当前时间")
    name = (req.name or "").strip()[:64] or None

    await db.execute(
        "INSERT INTO api_keys (id, key_hash, key_prefix, user_id, name, scopes, expires_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (kid, hash_key(raw), raw[:12], p.user_id, name, Jsonb(scopes), expires))

    log.info("api key created", extra=log_extra(user_id=p.user_id, key_id=kid))
    row = await db.fetch_one("SELECT * FROM api_keys WHERE id = %s", (kid,))
    return {**_key_public(row), "api_key": raw}  # 明文仅此一次


@router.get("")
async def list_keys(p: Principal = Depends(verify_api_key)):
    """列出当前用户的全部 API Key"""
    if p.is_static:
        raise HTTPException(401, "静态 Key 无归属用户，请先登录")
    rows = await db.fetch_all(
        "SELECT * FROM api_keys WHERE user_id = %s "
        "ORDER BY revoked ASC, created_at DESC", (p.user_id,))
    return {"keys": [_key_public(r) for r in rows]}


@router.delete("/{key_id}")
async def revoke_key(key_id: str, p: Principal = Depends(verify_api_key)):
    """吊销 API Key（立即失效，不可恢复）"""
    if p.is_static:
        raise HTTPException(401, "静态 Key 无归属用户，请先登录")
    row = await db.fetch_one(
        "UPDATE api_keys SET revoked = TRUE "
        "WHERE id = %s AND user_id = %s RETURNING id, revoked",
        (key_id, p.user_id))
    if not row:
        raise HTTPException(404, f"API Key 不存在: '{key_id}'")
    log.info("api key revoked", extra=log_extra(user_id=p.user_id, key_id=key_id))
    return {"id": key_id, "revoked": True}

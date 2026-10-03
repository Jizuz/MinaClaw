"""
PostgreSQL 连接池（psycopg3 async）

- DSN 兼容 jdbc:postgresql://host:port/db 与 postgresql://host:port/db
- 提供 fetch_one / fetch_all / execute 三个薄封装（dict 行、自动 commit）
- 由 main.py lifespan 负责初始化与关闭
"""
from typing import Any, Optional, Sequence

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from config import settings
from utils.logger import get_logger

log = get_logger("db")

_pool: Optional[AsyncConnectionPool] = None


def jdbc_to_dsn(url: str) -> str:
    """jdbc:postgresql://host:port/db → postgresql://host:port/db"""
    if url.startswith("jdbc:"):
        return url[len("jdbc:"):]
    return url


def pool_ready() -> bool:
    return _pool is not None and not _pool.closed


async def init_pool() -> None:
    global _pool
    if pool_ready():
        return
    dsn = jdbc_to_dsn(settings.database_url)
    log.info("db pool init", extra={"dsn": dsn})
    _pool = AsyncConnectionPool(
        conninfo=dsn,
        min_size=1,
        max_size=10,
        open=False,
        timeout=10,
    )
    await _pool.open(wait=True)
    log.info("db pool ready", extra={
        "min_size": _pool.min_size, "max_size": _pool.max_size})


async def close_pool() -> None:
    global _pool
    if _pool is not None and not _pool.closed:
        await _pool.close()
        log.info("db pool closed")
    _pool = None


def _check() -> AsyncConnectionPool:
    if _pool is None or _pool.closed:
        raise RuntimeError("数据库连接池未初始化（lifespan 中调用 init_pool）")
    return _pool


async def fetch_one(sql: str, params: Sequence[Any] = ()) -> Optional[dict]:
    async with _check().connection() as conn:
        conn.row_factory = dict_row
        cur = await conn.execute(sql, params)
        row = await cur.fetchone()
        await conn.commit()
        return row


async def fetch_all(sql: str, params: Sequence[Any] = ()) -> list[dict]:
    async with _check().connection() as conn:
        conn.row_factory = dict_row
        cur = await conn.execute(sql, params)
        rows = await cur.fetchall()
        await conn.commit()
        return rows


async def execute(sql: str, params: Sequence[Any] = ()) -> None:
    async with _check().connection() as conn:
        await conn.execute(sql, params)
        await conn.commit()

"""
数据库 Schema 初始化

启动时执行 ddl/base.sql（幂等：CREATE TABLE IF NOT EXISTS / DROP TRIGGER IF EXISTS），
正确处理 $$...$$ 函数体、单引号字符串、-- 注释内的分号。
"""
from pathlib import Path

from config import BASE_DIR
from db import pool as db
from utils.logger import get_logger, log_extra

log = get_logger("db.schema")

DDL_PATH = BASE_DIR / "ddl" / "base.sql"


def split_statements(sql_text: str) -> list[str]:
    """把 SQL 脚本拆分为可独立执行的语句列表（跳过注释与字符串中的分号）"""
    stmts: list[str] = []
    buf: list[str] = []
    i, n = 0, len(sql_text)
    in_dollar = False

    while i < n:
        # 行注释
        if not in_dollar and sql_text.startswith("--", i):
            j = sql_text.find("\n", i)
            i = n if j == -1 else j + 1
            continue

        # 单引号字符串（'' 转义）
        if not in_dollar and sql_text[i] == "'":
            j = i + 1
            while j < n:
                if sql_text[j] == "'":
                    if j + 1 < n and sql_text[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            buf.append(sql_text[i:min(j + 1, n)])
            i = j + 1
            continue

        # dollar-quoted 块（触发器函数体）
        if sql_text.startswith("$$", i):
            in_dollar = not in_dollar
            buf.append("$$")
            i += 2
            continue

        # 语句结束
        if not in_dollar and sql_text[i] == ";":
            stmt = "".join(buf).strip()
            if stmt:
                stmts.append(stmt)
            buf = []
            i += 1
            continue

        buf.append(sql_text[i])
        i += 1

    tail = "".join(buf).strip()
    if tail:
        stmts.append(tail)
    return stmts


async def init_schema() -> None:
    """执行 DDL 建表（可重复执行）+ 播种保留用户"""
    sql_text = DDL_PATH.read_text(encoding="utf-8")
    stmts = split_statements(sql_text)
    # 保留用户：静态 Key（dev-key-123 等）的会话归属，满足 sessions.user_id 外键
    stmts.append(
        "INSERT INTO users (id, username, display_name) "
        "VALUES ('static', 'static', '静态 Key（内置保留用户）') "
        "ON CONFLICT (id) DO NOTHING")
    log.info("init schema start", extra={"statements": len(stmts)})
    for stmt in stmts:
        await db.execute(stmt)
    log.info("init schema done", extra=log_extra(ddl=str(DDL_PATH)))

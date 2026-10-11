"""会话级加载记录与持久化验证（openspec: session-skill-load-cache 任务 2.1 / 2.2）

独立脚本直接运行：./claw/bin/python3 tests/test_session_record.py
DB 访问以 FakeDB 打桩（替换 core.session_manager.db 模块属性）
"""
import asyncio
import datetime
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import core.session_manager as sm_mod  # noqa: E402


class FakeDB:
    def __init__(self, session_row=None, messages=None):
        self.session_row = session_row
        self.messages = messages or []
        self.executed = []  # [(sql, params)]

    async def fetch_one(self, sql, params=None):
        return self.session_row

    async def fetch_all(self, sql, params=None):
        return self.messages

    async def execute(self, sql, params=None):
        self.executed.append((sql, params))


def make_row(meta=None):
    return {
        "id": "s1",
        "user_id": "u1",
        "title": "t",
        "summary": "",
        "created_at": datetime.datetime(2025, 1, 1, 12, 0, 0),
        "meta": meta,
    }


async def test_restore():
    # meta 为 NULL → 兜底为空
    db = FakeDB(session_row=make_row(meta=None))
    sm_mod.db = db
    m = sm_mod.SessionManager()
    sess = await m._load_history("s1")
    assert sess["loaded_skills"] == [], sess["loaded_skills"]
    assert sess["injected_skills"] == set()

    # meta 含合法记录 + 脏数据 → 仅恢复合法记录
    db = FakeDB(session_row=make_row(meta={"loaded_skills": [
        {"name": "pdf", "gen": 2},
        {"name": "bash", "gen": 3},
        {"junk": 1},        # 缺 name/gen，剔除
        {"name": "x"},      # 缺 gen，剔除
        "bad-type",         # 非 dict，剔除
    ]}))
    sm_mod.db = db
    m = sm_mod.SessionManager()
    sess = await m._load_history("s1")
    assert sess["loaded_skills"] == [
        {"name": "pdf", "gen": 2}, {"name": "bash", "gen": 3}]

    # exists() 路径：直接从 SELECT meta 恢复记录
    db = FakeDB(session_row=make_row(
        meta={"loaded_skills": [{"name": "pdf", "gen": 1}]}))
    sm_mod.db = db
    m = sm_mod.SessionManager()
    assert await m.exists("s1") is True
    assert m.sessions["s1"]["loaded_skills"] == [{"name": "pdf", "gen": 1}]
    print("restore ok")


async def test_record_and_clear():
    db = FakeDB()
    sm_mod.db = db
    m = sm_mod.SessionManager()
    sess = m._cache("s1", "u1", "", "", time.time(), True)

    await m.record_skill_load("s1", "pdf", 2)
    await m.record_skill_load("s1", "bash", 2)
    # 重复加载同名技能：去重 + 最近性置顶（移到列表末尾）
    await m.record_skill_load("s1", "pdf", 2)
    assert sess["loaded_skills"] == [
        {"name": "bash", "gen": 2}, {"name": "pdf", "gen": 2}]

    # 写穿：UPDATE SQL（jsonb_set + COALESCE 兜底）与参数正确
    sql, params = db.executed[-1]
    assert "jsonb_set" in sql, sql
    assert "COALESCE(meta, '{}'::jsonb)" in sql, sql
    assert "'{loaded_skills}'" in sql, sql
    expected = json.dumps([{"name": "bash", "gen": 2},
                           {"name": "pdf", "gen": 2}], ensure_ascii=False)
    assert params == (expected, "s1"), params

    # 清除记录并写穿
    await m.clear_skill_load("s1", "pdf")
    assert sess["loaded_skills"] == [{"name": "bash", "gen": 2}]
    sql, params = db.executed[-1]
    assert params == (json.dumps([{"name": "bash", "gen": 2}],
                                 ensure_ascii=False), "s1"), params

    # 记录不存在时 no-op（不产生新 UPDATE）；未知会话 no-op 不抛错
    n = len(db.executed)
    await m.clear_skill_load("s1", "ghost")
    await m.record_skill_load("nope", "pdf", 1)
    await m.clear_skill_load("nope", "pdf")
    assert len(db.executed) == n, "no-op 场景不应产生新的 UPDATE"
    print("record/clear ok")


def main():
    asyncio.run(test_restore())
    asyncio.run(test_record_and_clear())
    print("test_session_record ok")


if __name__ == "__main__":
    main()

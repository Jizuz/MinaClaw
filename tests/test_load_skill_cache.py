"""load_skill 命中去重四分支与热重载失效验证

（openspec: session-skill-load-cache 任务 4.1 / 4.2）

独立脚本直接运行：./claw/bin/python3 tests/test_load_skill_cache.py
DB 打桩；使用 session_manager 单例 + 假技能 registry
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import settings  # noqa: E402
from skills.skill_loader import Skill, registry  # noqa: E402
from skills.skill_executor import exec_load_skill  # noqa: E402
import core.session_manager as sm_mod  # noqa: E402
from core.session_manager import session_manager  # noqa: E402


class FakeDB:
    def __init__(self):
        self.executed = []

    async def fetch_one(self, sql, params=None):
        return None

    async def fetch_all(self, sql, params=None):
        return []

    async def execute(self, sql, params=None):
        self.executed.append((sql, params))


def make_skill(name, body):
    return Skill({"name": name, "description": f"{name} 摘要",
                  "executor": "template", "parameters": {}},
                 body, Path(f"defs/{name}/SKILL.md"))


def setup(gen=2, skills=None, loaded=None, injected=None):
    """返回 (db, sess)：重置单例会话与 registry"""
    db = FakeDB()
    sm_mod.db = db
    registry.skills = dict(skills or {})
    registry.generation = gen
    session_manager.sessions.clear()
    sess = session_manager._cache("s1", "u1", "", "", time.time(), True,
                                  loaded_skills=loaded or [])
    sess["injected_skills"] = set(injected or [])
    return db, sess


def persist_calls(db):
    return [c for c in db.executed if "loaded_skills" in c[0]]


async def test_branch1_no_session_or_switch_off():
    """分支 1：无 session_id / 开关关闭 → 现状全文，不记录"""
    a = make_skill("skill-a", "BODY-A")
    use = {"skill-a": a}

    db, sess = setup(skills=use)
    r = await exec_load_skill({"skill": "skill-a"}, {})
    assert r == {"success": True, "skill": "skill-a", "output": a.detail_text()}
    assert "cached" not in r
    assert sess["loaded_skills"] == [] and persist_calls(db) == []

    db, sess = setup(skills=use)
    with SettingsPatch(skill_session_cache=False):
        r = await exec_load_skill({"skill": "skill-a"},
                                  {"session_id": "s1"})
    assert r["output"] == a.detail_text() and "cached" not in r
    assert sess["loaded_skills"] == [] and persist_calls(db) == []

    # 未知技能 / 非法名行为不变
    r = await exec_load_skill({"skill": "ghost"}, {"session_id": "s1"})
    assert r["success"] is False and "available" in r
    r = await exec_load_skill({"skill": "load_skill"}, {"session_id": "s1"})
    assert r == {"success": False, "error": "无效的技能名"}
    print("branch1 ok")


async def test_branch2_missing_or_stale():
    """分支 2：记录缺失 / 世代失效 → 全文并按当前世代（重）记录"""
    a = make_skill("skill-a", "BODY-A")

    # 记录缺失（即使已在 injected 集合——如被裁剪场景后的重建）
    db, sess = setup(skills={"skill-a": a}, injected={"skill-a"})
    r = await exec_load_skill({"skill": "skill-a"}, {"session_id": "s1"})
    assert r["output"] == a.detail_text() and "cached" not in r
    assert sess["loaded_skills"] == [{"name": "skill-a", "gen": 2}]
    assert len(persist_calls(db)) == 1

    # 记录失效：gen=1 ≠ 当前世代 2
    db, sess = setup(skills={"skill-a": a},
                     loaded=[{"name": "skill-a", "gen": 1}],
                     injected={"skill-a"})
    r = await exec_load_skill({"skill": "skill-a"}, {"session_id": "s1"})
    assert r["output"] == a.detail_text() and "cached" not in r
    assert sess["loaded_skills"] == [{"name": "skill-a", "gen": 2}]
    print("branch2 ok")


class SettingsPatch:
    """临时覆盖 settings 属性（用后恢复）"""

    def __init__(self, **over):
        self.over = over
        self.saved = {}

    def __enter__(self):
        for k, v in self.over.items():
            self.saved[k] = getattr(settings, k)
            setattr(settings, k, v)
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            setattr(settings, k, v)
        return False


async def test_branch3_cache_hit():
    """分支 3：记录有效且已注入 → cached 简短提示，不再返回全文、不重记"""
    a = make_skill("skill-a", "BODY-A-DETAIL")
    b = make_skill("skill-b", "BODY-B")
    db, sess = setup(skills={"skill-a": a, "skill-b": b},
                     loaded=[{"name": "skill-b", "gen": 2},
                             {"name": "skill-a", "gen": 2}],
                     injected={"skill-a"})
    n_before = len(persist_calls(db))
    r = await exec_load_skill({"skill": "skill-a"}, {"session_id": "s1"})
    assert r["success"] is True and r.get("cached") is True, r
    assert r["skill"] == "skill-a"
    assert "BODY-A-DETAIL" not in r["output"], "命中缓存不应返回全文"
    assert "无需重复加载" in r["output"]
    # 记录与最近性不变，未产生新写穿
    assert sess["loaded_skills"] == [{"name": "skill-b", "gen": 2},
                                     {"name": "skill-a", "gen": 2}]
    assert len(persist_calls(db)) == n_before
    print("branch3 ok")


async def test_branch4_valid_but_not_injected():
    """分支 4：记录有效但未注入（超限列名/被裁剪）→ 全文并置顶最近性"""
    a = make_skill("skill-a", "BODY-A")
    b = make_skill("skill-b", "BODY-B")
    db, sess = setup(skills={"skill-a": a, "skill-b": b},
                     loaded=[{"name": "skill-b", "gen": 2},
                             {"name": "skill-a", "gen": 2}],
                     injected={"skill-a"})   # 仅 a 注入，b 超限列名
    r = await exec_load_skill({"skill": "skill-b"}, {"session_id": "s1"})
    assert r["output"] == b.detail_text() and "cached" not in r
    # b 的最近性被置顶：[b, a] -> [a, b]
    assert sess["loaded_skills"] == [{"name": "skill-a", "gen": 2},
                                     {"name": "skill-b", "gen": 2}]
    assert len(persist_calls(db)) == 1
    print("branch4 ok")


async def test_hot_reload_overwrite():
    """4.2 覆盖上传：世代自增后旧记录失效 → 返回新全文并按新世代重记"""
    a_v1 = make_skill("skill-a", "OLD-V1")
    db, sess = setup(gen=2, skills={"skill-a": a_v1},
                     loaded=[{"name": "skill-a", "gen": 2}],
                     injected={"skill-a"})
    # 模拟覆盖上传后的热重载：技能对象更新 + load_all 世代自增
    a_v2 = make_skill("skill-a", "NEW-V2-BODY")
    registry.skills["skill-a"] = a_v2
    registry.generation += 1

    r = await exec_load_skill({"skill": "skill-a"}, {"session_id": "s1"})
    assert r["success"] is True and "cached" not in r
    assert "NEW-V2-BODY" in r["output"] and "OLD-V1" not in r["output"]
    assert sess["loaded_skills"] == [{"name": "skill-a", "gen": 3}]

    # 新世代记录重新生效：注入后再次调用命中缓存
    sess["injected_skills"] = {"skill-a"}
    r = await exec_load_skill({"skill": "skill-a"}, {"session_id": "s1"})
    assert r.get("cached") is True
    print("hot reload overwrite ok")


async def test_hot_reload_delete():
    """4.2 删除：load_skill 返回未知技能；记录在下次 get_context 被清除"""
    a = make_skill("skill-a", "BODY-A")
    db, sess = setup(gen=2, skills={"skill-a": a},
                     loaded=[{"name": "skill-a", "gen": 2}])
    # 模拟删除后的热重载
    registry.skills.pop("skill-a")
    registry.generation += 1

    r = await exec_load_skill({"skill": "skill-a"}, {"session_id": "s1"})
    assert r["success"] is False and "未知技能" in r["error"]

    # 下一轮 get_context：无效记录被剔除并写穿清库
    sess["history"] = []
    await session_manager.get_context("s1", "SYS")
    assert sess["loaded_skills"] == []
    sql, params = db.executed[-1]
    assert "jsonb_set" in sql and params[0] == "[]"
    print("hot reload delete ok")


def main():
    asyncio.run(test_branch1_no_session_or_switch_off())
    asyncio.run(test_branch2_missing_or_stale())
    asyncio.run(test_branch3_cache_hit())
    asyncio.run(test_branch4_valid_but_not_injected())
    asyncio.run(test_hot_reload_overwrite())
    asyncio.run(test_hot_reload_delete())
    print("test_load_skill_cache ok")


if __name__ == "__main__":
    main()

"""轮首注入与 ctx 透传验证（openspec: session-skill-load-cache 任务 3.1 / 3.2 / 3.3）

独立脚本直接运行：./claw/bin/python3 tests/test_injection.py
DB 访问以 FakeDB 打桩；registry 注入假技能（不依赖磁盘 defs）
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import settings  # noqa: E402
from skills.skill_loader import Skill, registry  # noqa: E402
from skills import skill_executor as se  # noqa: E402
import core.session_manager as sm_mod  # noqa: E402
from core.session_manager import Message  # noqa: E402


class FakeDB:
    def __init__(self, session_row=None, messages=None):
        self.session_row = session_row
        self.messages = messages or []
        self.executed = []

    async def fetch_one(self, sql, params=None):
        return self.session_row

    async def fetch_all(self, sql, params=None):
        return self.messages

    async def execute(self, sql, params=None):
        self.executed.append((sql, params))


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


def make_skill(name, body):
    return Skill({"name": name, "description": f"{name} 测试技能摘要",
                  "executor": "template", "parameters": {}},
                 body, Path(f"defs/{name}/SKILL.md"))


def use_skills(skills, generation=2):
    registry.skills = dict(skills)
    registry.generation = generation


def injected_msgs(msgs):
    return [x for x in msgs
            if x["role"] == "system" and x["content"].startswith("[已加载技能]")]


async def test_inject_content():
    """3.1 注入内容：最近优先、注明无需再调用、写入 injected_skills"""
    a = make_skill("skill-a", "AAAA-" * 20)
    b = make_skill("skill-b", "BBBB-" * 20)
    use_skills({"skill-a": a, "skill-b": b}, generation=2)
    sm_mod.db = FakeDB()
    m = sm_mod.SessionManager()
    sess = m._cache("s1", "u1", "", "", time.time(), True,
                    loaded_skills=[{"name": "skill-a", "gen": 2},
                                   {"name": "skill-b", "gen": 2}])
    with SettingsPatch(skill_progressive=True, skill_session_cache=True,
                       skill_session_cache_max_chars=4000):
        msgs = await m.get_context("s1", "SYS-PROMPT")

    inj = injected_msgs(msgs)
    assert len(inj) == 1, f"应恰好一条注入消息，实际 {len(inj)}"
    text = inj[0]["content"]
    assert "## skill-a" in text and "## skill-b" in text
    assert text.index("## skill-b") < text.index("## skill-a"), "最近加载应在前"
    assert "无需再次调用 load_skill" in text
    assert sess["injected_skills"] == {"skill-a", "skill-b"}
    # 注入消息位于主系统提示之后、历史消息之前
    assert msgs[0] == {"role": "system", "content": "SYS-PROMPT"}
    assert msgs[1] is inj[0]
    print("inject content ok")


async def test_budget():
    """3.1 预算扣减：注入 tokens 计入 used，可选历史消息变少"""
    a = make_skill("big-skill", "X" * 3000)
    use_skills({"big-skill": a}, generation=2)

    def build():
        sm_mod.db = FakeDB()
        m = sm_mod.SessionManager()
        sess = m._cache("s1", "u1", "", "", time.time(), True,
                        loaded_skills=[{"name": "big-skill", "gen": 2}])
        for i in range(200):
            sess["history"].append(
                Message(role="user", content=f"msg-{i:03d} " + "y" * 200))
        return m

    with SettingsPatch(skill_session_cache=False):
        msgs_off = await build().get_context("s1", "SYS")
        n_off = sum(1 for x in msgs_off if x["role"] != "system")
    with SettingsPatch(skill_session_cache=True,
                       skill_session_cache_max_chars=4000):
        msgs_on = await build().get_context("s1", "SYS")
        n_on = sum(1 for x in msgs_on if x["role"] != "system")

    assert n_on < n_off, f"注入后可选历史应变少: {n_on} vs {n_off}"
    print(f"budget ok: 可选历史消息 {n_off} -> {n_on}")


async def test_over_limit():
    """3.1 超限：最近优先注入，超限技能仅列名提示"""
    a = make_skill("small-skill", "S" * 50)     # 详情约 70 字符
    b = make_skill("large-skill", "L" * 1000)   # 详情约 1020 字符
    use_skills({"small-skill": a, "large-skill": b}, generation=2)
    sm_mod.db = FakeDB()
    m = sm_mod.SessionManager()
    sess = m._cache("s1", "u1", "", "", time.time(), True,
                    loaded_skills=[{"name": "small-skill", "gen": 2},
                                   {"name": "large-skill", "gen": 2}])
    with SettingsPatch(skill_session_cache=True,
                       skill_session_cache_max_chars=100):
        msgs = await m.get_context("s1", "SYS")

    text = injected_msgs(msgs)[0]["content"]
    assert "## small-skill" in text, "未超限技能应注入详情"
    assert "## large-skill" not in text, "超限技能不应注入详情"
    assert "large-skill" in text and "load_skill" in text, "超限技能应列名提示"
    assert sess["injected_skills"] == {"small-skill"}, sess["injected_skills"]
    print("over-limit ok")


async def test_switch_off():
    """3.1 开关关闭（或非渐进式）：不注入、行为退化为现状、记录保留"""
    a = make_skill("skill-a", "AAAA")
    use_skills({"skill-a": a}, generation=2)
    sm_mod.db = FakeDB()
    m = sm_mod.SessionManager()
    sess = m._cache("s1", "u1", "", "", time.time(), True,
                    loaded_skills=[{"name": "skill-a", "gen": 2}])

    with SettingsPatch(skill_session_cache=False):
        msgs = await m.get_context("s1", "SYS")
    assert injected_msgs(msgs) == []
    assert sess["injected_skills"] == set()
    assert sess["loaded_skills"] == [{"name": "skill-a", "gen": 2}]

    with SettingsPatch(skill_progressive=False):
        msgs = await m.get_context("s1", "SYS")
    assert injected_msgs(msgs) == []
    print("switch off ok")


async def test_invalid_records():
    """3.3 记录有效性：世代不符 / 技能已删除 → 不注入并在内存剔除（含写穿）"""
    a = make_skill("skill-a", "AAAA")
    use_skills({"skill-a": a}, generation=2)
    db = FakeDB()
    sm_mod.db = db
    m = sm_mod.SessionManager()
    sess = m._cache("s1", "u1", "", "", time.time(), True,
                    loaded_skills=[{"name": "skill-a", "gen": 1},   # 世代不符
                                   {"name": "ghost", "gen": 2}])    # 技能已删除
    with SettingsPatch(skill_session_cache=True):
        msgs = await m.get_context("s1", "SYS")

    assert injected_msgs(msgs) == [], "无效记录不应注入"
    assert sess["loaded_skills"] == [], sess["loaded_skills"]
    sql, params = db.executed[-1]
    assert "jsonb_set" in sql and params[0] == "[]", (sql, params)
    print("invalid records ok")


async def test_ctx_passthrough():
    """3.2 ctx 透传：执行器收到的 ctx 含 session_id 与 _skill"""
    captured = {}

    async def probe(args, ctx):
        captured["args"] = args
        captured["ctx"] = ctx
        return {"success": True}

    se.EXECUTORS["__probe__"] = probe
    try:
        s = Skill({"name": "probe-skill", "description": "d",
                   "executor": "__probe__", "parameters": {}},
                  "", Path("defs/probe-skill/SKILL.md"))
        out = await s.run({"x": 1}, {"session_id": "s9"})
        assert out == {"success": True}
        assert captured["args"] == {"x": 1}
        assert captured["ctx"]["session_id"] == "s9"
        assert captured["ctx"]["_skill"]["name"] == "probe-skill"
        assert captured["ctx"]["_skill"]["executor_args"] == {}
    finally:
        del se.EXECUTORS["__probe__"]
    print("ctx passthrough ok")


def main():
    asyncio.run(test_inject_content())
    asyncio.run(test_budget())
    asyncio.run(test_over_limit())
    asyncio.run(test_switch_off())
    asyncio.run(test_invalid_records())
    asyncio.run(test_ctx_passthrough())
    print("test_injection ok")


if __name__ == "__main__":
    main()

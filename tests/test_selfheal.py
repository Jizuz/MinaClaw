"""工具异常自愈验证（openspec: tool-error-self-healing 任务 1-4）

独立脚本直接运行：./claw/bin/python3 tests/test_selfheal.py
chat_stream 以脚本轮次打桩；registry 注入假技能（不依赖磁盘 defs 与 LLM）
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx  # noqa: E402

import core.agent_loop as ag  # noqa: E402
from config import settings  # noqa: E402
from skills.skill_loader import Skill, registry  # noqa: E402
from skills import skill_executor as se  # noqa: E402

EV_FINISH = {"type": "finish"}


def ev_tc(name, args_str, i=0):
    return {"type": "tool_call", "index": i, "id": f"call-{name}-{i}",
            "name": name, "arguments_delta": args_str}


def ev_content(text):
    return {"type": "content", "delta": text}


class ScriptedLLM:
    """按轮次弹出事件脚本的假 chat_stream（调用次数可查）"""

    def __init__(self, rounds):
        self.rounds = [list(r) for r in rounds]
        self.calls = 0

    def __call__(self, messages, tools=None, on_usage=None):
        self.calls += 1
        evs = self.rounds.pop(0) if self.rounds else [
            ev_content("（无脚本）"), EV_FINISH]

        async def gen():
            for e in evs:
                yield e
        return gen()


class FakeMeter:
    def record(self, **kw):
        return 0, False


async def run_agent(rounds):
    """打桩 chat_stream/token_meter 后跑一轮任务，返回 (llm, messages, events)"""
    llm = ScriptedLLM(rounds)
    old_stream, old_meter = ag.chat_stream, ag.token_meter
    ag.chat_stream = llm
    ag.token_meter = FakeMeter()
    agent = ag.ClawAgent()
    messages = [{"role": "user", "content": "hi"}]
    try:
        events = []
        async for ev in agent.run(messages, "k", "test-session"):
            events.append(ev)
    finally:
        ag.chat_stream = old_stream
        ag.token_meter = old_meter
    return llm, messages, events


def tool_results(events):
    return [e["result"] for e in events if e["type"] == "tool_end"]


def install_skill(name, executor):
    skill = Skill({"name": name, "description": f"{name} 测试摘要",
                   "executor": executor, "parameters": {}},
                  "", Path(f"defs/{name}/SKILL.md"))
    registry.skills = {name: skill}
    registry.generation = 2
    return skill


def install_skills(named):
    """named: {工具名: executor}"""
    registry.skills = {
        n: Skill({"name": n, "description": f"{n} 测试摘要",
                  "executor": ex, "parameters": {}},
                 "", Path(f"defs/{n}/SKILL.md"))
        for n, ex in named.items()
    }
    registry.generation = 2


def raising_exc(exc):
    async def run(args, ctx):
        raise exc
    return run


def scripted_outcomes(outcomes):
    """按调用序返回 outcomes 中的元素（Exception 则抛出）；调用数可查"""
    state = {"n": 0}

    async def run(args, ctx):
        i = state["n"]
        state["n"] += 1
        out = outcomes[min(i, len(outcomes) - 1)]
        if isinstance(out, Exception):
            raise out
        return dict(out)  # 返回拷贝：模拟真实执行器每次新 dict
    run.state = state
    return run


FAIL = {"success": False, "error": "x", "output": ""}
OK = {"success": True, "output": "ok"}


# ---------- 任务 1：错误信封与解析错误回传 ----------

async def test_invalid_args():
    """1.1 坏 JSON：不执行、kind=invalid_args、raw 片段、tool 消息回传"""
    se.EXECUTORS["__probe__"] = scripted_outcomes([OK])
    try:
        install_skill("demo_skill", "__probe__")
        bad = '{"path": "a.txt",'
        llm, messages, events = await run_agent([
            [ev_tc("demo_skill", bad), EV_FINISH],
            [ev_content("已修正"), EV_FINISH],
        ])
        rs = tool_results(events)
        assert len(rs) == 1, f"应恰有一次 tool_end，实际 {len(rs)}"
        r = rs[0]
        assert r.get("success") is False
        assert r.get("kind") == "invalid_args", r
        assert r.get("error"), "error 应含解析细节且非空"
        assert r.get("retry_hint"), "应携带修复指引"
        assert r.get("raw") == bad, "应回传原始参数片段"
        assert se.EXECUTORS["__probe__"].state["n"] == 0, "坏参数不应执行工具"
        tool_msg = [m for m in messages if m["role"] == "tool"][-1]
        assert "invalid_args" in tool_msg["content"]
        assert events[-1]["type"] == "done"
        print("invalid args ok")
    finally:
        del se.EXECUTORS["__probe__"]

async def test_empty_exc_fallback():
    """1.1 空消息异常：error 回退为异常类名"""
    se.EXECUTORS["__raise__"] = raising_exc(RuntimeError())
    try:
        install_skill("demo_skill", "__raise__")
        _, _, events = await run_agent([
            [ev_tc("demo_skill", "{}"), EV_FINISH],
            [ev_content("完成"), EV_FINISH],
        ])
        r = tool_results(events)[0]
        assert r.get("success") is False
        assert r.get("error") == "RuntimeError", r
        print("empty exc fallback ok")
    finally:
        del se.EXECUTORS["__raise__"]


async def test_long_error_truncated():
    """1.1 超长错误：截断至 500 字符"""
    se.EXECUTORS["__raise__"] = raising_exc(Exception("E" * 1000))
    try:
        install_skill("demo_skill", "__raise__")
        _, _, events = await run_agent([
            [ev_tc("demo_skill", "{}"), EV_FINISH],
            [ev_content("完成"), EV_FINISH],
        ])
        r = tool_results(events)[0]
        assert len(r.get("error", "")) <= 500, len(r.get("error", ""))
        print("long error truncated ok")
    finally:
        del se.EXECUTORS["__raise__"]


# ---------- 任务 2：未知工具与异常分类 ----------

async def test_unknown_tool():
    """2.1 未知工具：kind=unknown_tool 且附 available 列表"""
    install_skills({"demo_skill": "__unused__"})
    _, _, events = await run_agent([
        [ev_tc("ghost_tool", "{}"), EV_FINISH],
        [ev_content("完成"), EV_FINISH],
    ])
    r = tool_results(events)[0]
    assert r.get("kind") == "unknown_tool", r
    assert isinstance(r.get("available"), list), r
    assert "demo_skill" in r["available"], r["available"]
    assert r.get("retry_hint"), "应携带修复指引"
    print("unknown tool ok")


async def test_kind_mapping():
    """2.1 异常分类：timeout / transient / permanent 各携 retry_hint"""
    exc_map = {
        "t_timeout": asyncio.TimeoutError("slow"),
        "t_transient": httpx.ConnectError("net down"),
        "t_permanent": ValueError("bad input"),
    }

    async def raise_by_skill(args, ctx):
        raise exc_map[ctx["_skill"]["name"]]

    se.EXECUTORS["__raise__"] = raise_by_skill
    try:
        install_skills({n: "__raise__" for n in exc_map})
        _, _, events = await run_agent([
            [ev_tc("t_timeout", "{}"), EV_FINISH],
            [ev_tc("t_transient", "{}"), EV_FINISH],
            [ev_tc("t_permanent", "{}"), EV_FINISH],
            [ev_content("完成"), EV_FINISH],
        ])
        rs = tool_results(events)
        kinds = [r.get("kind") for r in rs]
        assert kinds == ["timeout", "transient", "permanent"], kinds
        for r in rs:
            assert r.get("retry_hint"), r
        print("kind mapping ok")
    finally:
        del se.EXECUTORS["__raise__"]


# ---------- 任务 3：失败防抖 ----------

async def test_escalation():
    """3.1 连续失败 2 次：第二次结果附加策略提示"""
    se.EXECUTORS["__flaky__"] = scripted_outcomes([FAIL, FAIL])
    try:
        install_skill("flaky_skill", "__flaky__")
        _, _, events = await run_agent([
            [ev_tc("flaky_skill", "{}"), EV_FINISH],
            [ev_tc("flaky_skill", "{}"), EV_FINISH],
            [ev_content("放弃"), EV_FINISH],
        ])
        rs = tool_results(events)
        assert "escalation" not in rs[0], rs[0]
        assert "已连续失败 2 次" in rs[1].get("escalation", ""), rs[1]
        assert events[-1]["type"] == "done"
        print("escalation ok")
    finally:
        del se.EXECUTORS["__flaky__"]


async def test_terminate():
    """3.1 连续失败达 TOOL_MAX_FAILURES（默认 3）：error 终止且无下一轮"""
    se.EXECUTORS["__flaky__"] = scripted_outcomes([FAIL])
    try:
        install_skill("flaky_skill", "__flaky__")
        llm, _, events = await run_agent([
            [ev_tc("flaky_skill", "{}"), EV_FINISH],
            [ev_tc("flaky_skill", "{}"), EV_FINISH],
            [ev_tc("flaky_skill", "{}"), EV_FINISH],
        ])
        assert llm.calls == 3, f"第 3 次失败后不应再调用 LLM，实际 {llm.calls}"
        assert len(tool_results(events)) == 3
        errs = [e for e in events if e["type"] == "error"]
        assert errs and "3 次" in errs[0].get("message", ""), events
        assert not any(e["type"] == "done" for e in events)
        print("terminate ok")
    finally:
        del se.EXECUTORS["__flaky__"]


async def test_reset_on_success():
    """3.1 成功后计数清零：再失败从 1 起算，无 escalation"""
    se.EXECUTORS["__flaky__"] = scripted_outcomes(
        [FAIL, FAIL, OK, FAIL])
    try:
        install_skill("flaky_skill", "__flaky__")
        _, _, events = await run_agent([
            [ev_tc("flaky_skill", "{}"), EV_FINISH],
            [ev_tc("flaky_skill", "{}"), EV_FINISH],
            [ev_tc("flaky_skill", "{}"), EV_FINISH],
            [ev_tc("flaky_skill", "{}"), EV_FINISH],
            [ev_content("结束"), EV_FINISH],
        ])
        rs = tool_results(events)
        assert "已连续失败 2 次" in rs[1].get("escalation", ""), rs[1]
        assert "escalation" not in rs[3], f"成功后应清零计数：{rs[3]}"
        assert events[-1]["type"] == "done"
        print("reset on success ok")
    finally:
        del se.EXECUTORS["__flaky__"]


# ---------- 任务 4：配置与提示词 ----------

def test_config_default():
    """4.1 TOOL_MAX_FAILURES 默认 3"""
    assert settings.tool_max_failures == 3, settings.tool_max_failures
    print("config default ok")


def test_prompt_strategy():
    """4.2 系统提示词含自愈策略要点"""
    p = ag.SYSTEM_PROMPT
    for kw in ("工具失败", "重试", "相同"):
        assert kw in p, f"系统提示词缺少关键要点: {kw}"
    print("prompt strategy ok")


def main():
    asyncio.run(test_invalid_args())
    asyncio.run(test_empty_exc_fallback())
    asyncio.run(test_long_error_truncated())
    asyncio.run(test_unknown_tool())
    asyncio.run(test_kind_mapping())
    asyncio.run(test_escalation())
    asyncio.run(test_terminate())
    asyncio.run(test_reset_on_success())
    test_config_default()
    test_prompt_strategy()
    print("test_selfheal ok")


if __name__ == "__main__":
    main()


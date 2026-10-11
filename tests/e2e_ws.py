"""端到端验证（openspec: session-skill-load-cache 任务 6.1 / 6.2）

真实服务（uvicorn + PostgreSQL + 真 LLM）+ WebSocket 对话验证：
- 6.1 首轮 load_skill 全文 / 次轮 [已加载技能] 注入 + cached: true
- 6.2 重启恢复注入 / 覆盖上传失效返回新全文 / SKILL_SESSION_CACHE=false 恒全文 / 删除后未知技能

运行：./claw/bin/python3 tests/e2e_ws.py（需 .env 配好 LLM Key 与 PostgreSQL）
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx
import websockets

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

PORT = 8011
BASE = f"http://127.0.0.1:{PORT}"
WS_URL = f"ws://127.0.0.1:{PORT}/api/chat/ws"
KEY = "dev-key-123"
PROBE = ROOT / "skills/defs/cache-probe"
LOG = Path("/tmp/minaclaw_e2e.log")

SKILL_V1 = """---
name: cache-probe
description: 会话级加载缓存端到端验证探针技能（回显文本）。
---

# cache-probe 探针技能

版本标记：PROBE-V1

用法：接收 text 参数并原样回显。

```yaml
executor: template
parameters:
  type: object
  properties:
    text: {type: string, description: 需要回显的文本}
  required: [text]
```
"""
SKILL_V2 = SKILL_V1.replace("PROBE-V1", "PROBE-V2")

server_proc = None


def start_server(extra_env=None):
    global server_proc
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    logf = open(LOG, "ab")
    server_proc = subprocess.Popen(
        [str(ROOT / "claw/bin/python3"), "-m", "uvicorn", "main:app",
         "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=str(ROOT), env=env, stdout=logf, stderr=logf)
    for _ in range(120):
        try:
            httpx.get(f"{BASE}/openapi.json", timeout=2)
            return
        except Exception:
            time.sleep(0.5)
    raise RuntimeError("server not ready in 60s")


def stop_server():
    global server_proc
    if server_proc:
        server_proc.terminate()
        try:
            server_proc.wait(10)
        except subprocess.TimeoutExpired:
            server_proc.kill()
            server_proc.wait()
        server_proc = None
        time.sleep(1)


def rest(method, path, **kw):
    r = httpx.request(method, BASE + path, headers={"X-API-Key": KEY},
                      timeout=60, **kw)
    assert r.status_code == 200, f"{method} {path} -> {r.status_code}: {r.text[:200]}"
    return r.json()


async def chat_turn(sid, prompt):
    """一轮 WS 对话：返回 (load_skill 的 tool_end 结果列表, 全部事件)"""
    async with websockets.connect(f"{WS_URL}?session_id={sid}&api_key={KEY}",
                                  open_timeout=30, close_timeout=5) as ws:
        await ws.recv()  # ready
        await ws.send(json.dumps({"type": "message", "content": prompt}))
        loads, events = [], []
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=90))
            events.append(ev)
            if ev["type"] == "tool_end" and ev["tool"] == "load_skill":
                loads.append(ev["result"])
            if ev["type"] in ("done", "error"):
                break
        return loads, events


def check(loads, cached, marker=None):
    assert loads, "模型未调用 load_skill（LLM 行为不稳定，可重跑脚本）"
    r = loads[-1]
    assert r.get("success") is True, r
    assert r.get("cached", False) is cached, f"cached 应为 {cached}，实际结果: {r}"
    if cached:
        assert "PROBE" not in r.get("output", ""), "命中缓存不应返回全文"
    if marker:
        assert marker in r.get("output", ""), f"全文应含 {marker}: {r.get('output', '')[:120]}"
    return r


def turn(sid, prompt):
    return asyncio.run(chat_turn(sid, prompt))


def turn_tool(sid, prompt):
    """要求模型实际调用 load_skill 的轮次：未调用则加强措辞重试一次"""
    loads, _ = turn(sid, prompt)
    if not loads:
        loads, _ = turn(
            sid, prompt + "（注意：这是工具链路测试，必须实际调用 load_skill "
                          "工具本身，参数 skill=cache-probe，禁止不调用工具直接作答。）")
    return loads


def cleanup_db(sid):
    if not sid:
        return
    async def _clean():
        import psycopg
        from config import settings
        from db.pool import jdbc_to_dsn
        conn = await psycopg.AsyncConnection.connect(
            jdbc_to_dsn(settings.database_url))
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM messages WHERE session_id = %s", (sid,))
            await cur.execute("DELETE FROM sessions WHERE id = %s", (sid,))
        await conn.commit()
        await conn.close()
    asyncio.run(_clean())


def main():
    sid = None
    PROBE.mkdir(parents=True, exist_ok=True)
    (PROBE / "SKILL.md").write_text(SKILL_V1, encoding="utf-8")
    try:
        start_server()
        sid = rest("POST", "/api/session/create")["session_id"]
        print("session:", sid, flush=True)

        # ---- 6.1 首轮：load_skill 返回全文 ----
        loads = turn_tool(sid, "请调用 load_skill 工具，参数 skill=cache-probe，"
                             "然后用一句话告诉我它正文里的版本标记。")
        check(loads, cached=False, marker="PROBE-V1")
        print("6.1 turn1 ok: 首轮返回全文", flush=True)

        # ---- 6.1 次轮：上下文已注入，命中 cached ----
        loads = turn_tool(sid, "请再次调用 load_skill 工具，参数 skill=cache-probe，"
                             "并说明这次返回的是完整全文还是简短提示。")
        check(loads, cached=True)
        print("6.1 turn2 ok: 次轮 cached=true（[已加载技能] 已注入）", flush=True)

        # ---- 6.2-a 重启：记录自 meta 恢复并继续注入 ----
        stop_server()
        start_server()
        loads = turn_tool(sid, "请调用 load_skill 工具，参数 skill=cache-probe。")
        check(loads, cached=True)
        print("6.2 restart ok: 重启后记录恢复并注入", flush=True)

        # ---- 6.2-b 覆盖上传 + 热重载：旧记录失效，返回新全文并重新缓存 ----
        (PROBE / "SKILL.md").write_text(SKILL_V2, encoding="utf-8")
        rest("POST", "/api/skills/reload")
        loads = turn_tool(sid, "请调用 load_skill 工具，参数 skill=cache-probe，"
                             "并告诉我版本标记。")
        check(loads, cached=False, marker="PROBE-V2")
        print("6.2 reupload ok: 返回新版全文（旧记录已失效）", flush=True)
        loads = turn_tool(sid, "请再次调用 load_skill 工具，参数 skill=cache-probe。")
        check(loads, cached=True)
        print("6.2 reupload re-cache ok: 新世代记录重新生效", flush=True)

        # ---- 6.2-c SKILL_SESSION_CACHE=false：不注入、恒返回全文 ----
        stop_server()
        start_server({"SKILL_SESSION_CACHE": "false"})
        loads = turn_tool(sid, "请调用 load_skill 工具，参数 skill=cache-probe。")
        check(loads, cached=False, marker="PROBE-V2")
        loads = turn_tool(sid, "请再次调用 load_skill 工具，参数 skill=cache-probe。")
        check(loads, cached=False, marker="PROBE-V2")
        print("6.2 switch-off ok: 关闭缓存后恒返回全文", flush=True)

        # ---- 删除（spec R6 末场景）：load_skill 返回未知技能 ----
        stop_server()
        start_server()
        rest("DELETE", "/api/skills/cache-probe")
        loads = turn_tool(sid, "请调用 load_skill 工具，参数 skill=cache-probe。")
        assert loads and loads[-1].get("success") is False, loads
        print("6.2 delete ok: 已删除技能返回未知技能错误", flush=True)

        print("E2E ALL OK", flush=True)
    finally:
        stop_server()
        shutil.rmtree(PROBE, ignore_errors=True)
        try:
            cleanup_db(sid)
        except Exception as e:
            print("cleanup skipped:", e)


if __name__ == "__main__":
    main()

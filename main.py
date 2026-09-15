"""
MinaClaw Agent Gateway

- WebSocket 流式对话
- 流量检测 + 任务队列
- 结构化日志 + Token 检测
"""
import asyncio

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from utils.logger import (setup_logging, get_logger, get_audit_logger, bind_context, reset_context, log_extra)
from middleware.auth import verify_api_key, get_valid_keys

from core.agent_loop import agent, SYSTEM_PROMPT
from skills.skill_loader import registry
from core.session_manager import session_manager
from middleware.rate_limit import traffic
from core.task_queue import task_queue
from utils.token_counter import token_meter, count_tokens

from api.routes.skills import router as SkillRouter
from api.routes.session import router as SessionRouter
from api.routes.agent import router as AgentRouter

# ---- 日志初始化（必须在其他模块前）----
setup_logging()
log = get_logger("main")
audit = get_audit_logger()


# ==================== Agent Runner ====================
async def agent_runner(session_id: str, 
                       api_key: str,
                       payload: dict, 
                       event_queue: asyncio.Queue,
                       task_id: str = ""):
    """由 task_queue worker 调用，把 agent 事件推入 event_queue"""
    tokens = bind_context(session_id=session_id, api_key=api_key, task_id=task_id)
    try:
        user_text = payload["content"]
        log.info("agent_runner start", extra=log_extra(content_len=len(user_text)))

        session_manager.append(session_id, "user", user_text)
        audit.info("user_message", extra=log_extra(content=user_text[:500]))

        messages = session_manager.get_context(session_id, SYSTEM_PROMPT)

        final_text = ""
        success = True
        try:
            async for ev in agent.run(messages, api_key=api_key, session_id=session_id):
                if ev["type"] == "thinking":
                    final_text += ev["delta"]
                if ev["type"] == "error":
                    success = False
                await event_queue.put(ev)
        except asyncio.CancelledError:
            await event_queue.put({"type": "cancelled"})
            raise
        except Exception as e:
            success = False
            log.exception("agent_runner error")
            await event_queue.put({"type": "error", "message": str(e)})
        finally:
            if final_text:
                session_manager.append(session_id, "assistant", final_text)
                audit.info("assistant_message", extra=log_extra(content=final_text[:500]))
            await traffic.release(success)
            log.info("agent_runner done", extra=log_extra(success=success, reply_len=len(final_text)))
    finally:
        reset_context(tokens)


# ==================== 生命周期 ====================
@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("startup: loading skills")
    registry.load_all()
    agent.refresh_tools()
    log.info("startup: skills ready", extra=log_extra(skills=list(registry.skills.keys())))

    await task_queue.start(agent_runner)
    log.info("startup: task_queue ready", extra=log_extra(workers=task_queue.workers, capacity=task_queue.maxsize))

    yield

    await task_queue.stop()
    log.info("shutdown done")


app = FastAPI(title="MinaClaw Agent Gateway", version="2.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"], 
    allow_headers=["*"],
)

# ==================== Agent Starter ====================
app.include_router(SkillRouter)
app.include_router(SessionRouter)
app.include_router(AgentRouter)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8010, reload=True)
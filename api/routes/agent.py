import asyncio
import json
import uuid

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect

from utils.logger import (setup_logging, get_logger, bind_context, reset_context, log_extra)
from core.session_manager import session_manager
from middleware.auth import get_valid_keys, verify_api_key
from core.task_queue import task_queue
from middleware.rate_limit import traffic
from utils.token_counter import token_meter
from core.agent_loop import SYSTEM_PROMPT
from utils.logger import bind_context, get_logger, log_extra, reset_context

router = APIRouter(prefix="/api/chat", tags=["聊天管理"])

# setup_logging()
log = get_logger("agent")

@router.get("/traffic")
async def traffic_stats(_key: str = Depends(verify_api_key)):
    return traffic.snapshot()


@router.get("/queue")
async def queue_stats(_key: str = Depends(verify_api_key)):
    return task_queue.snapshot()


@router.get("/tokens")
async def token_stats(
    session_id: str | None = None,
    key: str = Depends(verify_api_key),
):
    return token_meter.snapshot(api_key=key, session_id=session_id)

@router.websocket("/ws")
async def ws_chat(
    websocket: WebSocket,
    session_id: str = Query(...),
    api_key: str = Query(...),
):
    log.info(f"=====> ws_chat start, session_id: {session_id}")
    request_id = uuid.uuid4().hex[:8]
    tokens = bind_context(request_id=request_id, api_key=api_key, session_id=session_id)
    try:
        if api_key not in get_valid_keys():
            await websocket.close(code=4001, reason="Unauthorized")
            return

        if not session_manager.exists(session_id):
            session_id = session_manager.create()
            log.info("session auto-created", extra=log_extra(session_id=session_id))

        await websocket.accept()
        await websocket.send_json({"type": "ready", "session_id": session_id})
        log.info("ws connected")

        event_queue: asyncio.Queue = asyncio.Queue()
        current_task_id: dict = {"value": None}
        stop = asyncio.Event()

        async def pump_events():
            while not stop.is_set():
                try:
                    ev = await asyncio.wait_for(event_queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                if ev.get("type") == "__eos__":
                    current_task_id["value"] = None
                    continue
                try:
                    await websocket.send_json(ev)
                except Exception:
                    return

        pump = asyncio.create_task(pump_events())

        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    await websocket.send_json({"type": "error", "message": "非法 JSON"})
                    continue

                if payload.get("type") == "ping":
                    await websocket.send_json({"type": "pong"})
                    continue

                if payload.get("type") == "interrupt":
                    tid = current_task_id["value"]
                    if tid:
                        await task_queue.cancel(tid)
                    continue

                if payload.get("type") != "message":
                    continue

                user_text = (payload.get("content") or "").strip()
                if not user_text:
                    continue

                # 1) 流量检测
                ok, reason = await traffic.check(api_key)
                if not ok:
                    log.warning("traffic reject", extra=log_extra(reason=reason))
                    await websocket.send_json({
                        "type": "error",
                        "message": f"请求被限流: {reason}",
                        "reject": reason,
                    })
                    continue

                # 2) Token 配额检查
                ok_quota, used, quota = token_meter.check_quota(api_key)
                if not ok_quota:
                    await traffic.release(success=True)
                    await websocket.send_json({
                        "type": "error",
                        "message": f"今日 Token 配额已用尽 ({used}/{quota})",
                        "reject": "token_quota_exceeded",
                    })
                    continue

                # 3) 提交任务
                task = await task_queue.submit(
                    session_id=session_id,
                    api_key=api_key,
                    payload={"content": user_text},
                    priority=10,
                    event_queue=event_queue,
                )

                if task is None:
                    await traffic.release(success=True)
                    await websocket.send_json({
                        "type": "error",
                        "message": "服务器繁忙，请稍后重试",
                        "reject": "queue_full",
                    })
                    continue

                current_task_id["value"] = task.task_id
                await websocket.send_json({
                    "type": "queued",
                    "task_id": task.task_id,
                    "queue": task_queue.snapshot(),
                })

        except WebSocketDisconnect:
            log.info("ws disconnected")
        finally:
            stop.set()
            pump.cancel()
            await task_queue.cancel_by_session(session_id)
    finally:
        reset_context(tokens)
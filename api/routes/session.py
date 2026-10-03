from fastapi import APIRouter, Depends
from pydantic import BaseModel

from core.session_manager import session_manager
from middleware.auth import Principal, verify_api_key
from utils.logger import get_logger, log_extra

router = APIRouter(prefix="/api/session", tags=["会话管理"])

log = get_logger("session")

class NewSessionResp(BaseModel):
    session_id: str

@router.post("/create", response_model=NewSessionResp)
async def create_session(p: Principal = Depends(verify_api_key)):
    sid = await session_manager.create(user_id=p.user_id)
    log.info("session created",
             extra=log_extra(session_id=sid, user_id=p.user_id))
    return NewSessionResp(session_id=sid)


@router.get("/list")
async def list_sessions(p: Principal = Depends(verify_api_key)):
    return {"sessions": await session_manager.list_sessions(user_id=p.user_id)}


@router.get("/{sid}/messages")
async def get_messages(sid: str, p: Principal = Depends(verify_api_key)):
    """读取会话历史消息（校验归属，越权返回空列表）"""
    return {"messages": await session_manager.get_messages(sid, user_id=p.user_id)}


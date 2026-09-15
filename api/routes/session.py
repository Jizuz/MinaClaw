from fastapi import APIRouter, Depends
from pydantic import BaseModel

from core.session_manager import session_manager
from middleware.auth import verify_api_key
from utils.logger import get_logger, log_extra

router = APIRouter(prefix="/api/session", tags=["会话管理"])

log = get_logger("session")

class NewSessionResp(BaseModel):
    session_id: str
    
@router.post("/create", response_model=NewSessionResp)
async def create_session(_key: str = Depends(verify_api_key)):
    sid = session_manager.create()
    log.info("session created", extra=log_extra(session_id=sid))
    return NewSessionResp(session_id=sid)


@router.get("/list")
async def list_sessions(_key: str = Depends(verify_api_key)):
    return {"sessions": session_manager.list_sessions()}


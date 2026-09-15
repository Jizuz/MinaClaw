from pydantic import BaseModel
import uuid
from typing import Optional, Dict, Any, List

class ClawMessage(BaseModel):
    msg_id: str
    role: str # user / assistant / tool
    content: str
    tool_call: Optional[Dict[str,Any]] = None

class AgentSubmitRequest(BaseModel):
    session_id: Optional[str] = None
    prompt: str
    stream: bool = False

class SessionCreateResp(BaseModel):
    session_id: str
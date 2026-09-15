from logging import log

from fastapi import APIRouter, Depends

from middleware.auth import verify_api_key
from skills.skill_loader import registry
from core.agent_loop import agent, SYSTEM_PROMPT
from utils.logger import get_logger, log_extra

router = APIRouter(prefix="/api/skills", tags=["技能管理"])

@router.get("/list")
async def list_skills(_key: str = Depends(verify_api_key)):
    return {"skills": [
        {"name": s.name, "description": s.description,
         "executor": s.executor_name, "parameters": s.parameters}
        for s in registry.skills.values()
    ]}

@router.post("/reload")
async def reload_skills(_key: str = Depends(verify_api_key)):
    registry.load_all()
    agent.refresh_tools()
    log.info("skills reloaded",
             extra=log_extra(skills=list(registry.skills.keys())))
    return {"skills": list(registry.skills.keys())}
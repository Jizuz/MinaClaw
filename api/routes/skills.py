from fastapi import (APIRouter, Depends, File, Form, HTTPException,
                     UploadFile)

from config import settings
from middleware.auth import verify_api_key
from skills.skill_loader import registry
from skills.skill_uploader import UploadError, delete_skill, upload_skill
from core.agent_loop import agent
from utils.logger import get_logger, log_extra

log = get_logger("skills.api")

router = APIRouter(prefix="/api/skills", tags=["技能管理"])

@router.get("/list")
async def list_skills(_key: str = Depends(verify_api_key)):
    return {
        "progressive": settings.skill_progressive,
        "skills": [
            {"name": s.name, "description": s.description,
             "executor": s.executor_name, "parameters": s.parameters,
             "detail_chars": len(s.detail_text()),
             "source": "dir" if s.source.name == "SKILL.md" else "file",
             "assets": [str(p.relative_to(s.source.parent)) for p in s.assets]}
            for s in registry.skills.values()
        ],
    }

@router.post("/reload")
async def reload_skills(_key: str = Depends(verify_api_key)):
    registry.load_all()
    agent.refresh_tools()
    log.info("skills reloaded",
             extra=log_extra(skills=list(registry.skills.keys())))
    return {"skills": list(registry.skills.keys())}

@router.post("/upload")
async def upload_skill_zip(
    file: UploadFile = File(
        ..., description="技能目录包 .zip：根级 SKILL.md 或 <name>/SKILL.md，一次一个技能"),
    overwrite: bool = Form(False, description="同名技能是否覆盖"),
    _key: str = Depends(verify_api_key),
):
    """上传 zip 技能目录包，校验通过后落位 defs/ 并热加载生效"""
    if not (file.filename or "").lower().endswith(".zip"):
        raise HTTPException(400, "仅支持 .zip 技能目录包")
    data = await file.read()
    try:
        info = upload_skill(data, overwrite=overwrite)
    except UploadError as e:
        raise HTTPException(e.status_code, str(e))
    registry.load_all()
    agent.refresh_tools()
    return info

@router.delete("/{name}")
async def remove_skill(name: str, _key: str = Depends(verify_api_key)):
    """删除指定技能（目录/单文件形式）并热加载生效"""
    try:
        result = delete_skill(name)
    except UploadError as e:
        raise HTTPException(e.status_code, str(e))
    registry.load_all()
    agent.refresh_tools()
    return result

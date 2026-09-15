from fastapi import Header, HTTPException
from config import settings


def get_valid_keys() -> set[str]:
    return {k.strip() for k in settings.minaclaw_api_keys.split(",") if k.strip()}


async def verify_api_key(x_api_key: str = Header(default="", alias="X-API-Key")):
    if not x_api_key or x_api_key not in get_valid_keys():
        raise HTTPException(status_code=401, detail="Invalid API Key")
    return x_api_key
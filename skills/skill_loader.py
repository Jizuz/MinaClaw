import re
from pathlib import Path
from typing import Dict, Any, List, Optional
import yaml

from skills.skill_executor import get_executor
from utils.logger import get_logger, log_extra

log = get_logger("skills")

DEF_DIR = Path(__file__).parent / "defs"


class Skill:
    def __init__(self, meta: Dict[str, Any], body: str, source: Path):
        self.meta = meta
        self.body = body
        self.source = source

        self.name: str = meta["name"]
        self.description: str = meta["description"]
        self.parameters: Dict = meta.get("parameters", {})
        self.executor_name: str = meta["executor"]
        self.executor_args: Dict = meta.get("executor_args", {}) or {}
        self.extra = {k: v for k, v in meta.items()
                      if k not in ("name", "description",
                                   "parameters", "executor", "executor_args")}

    def to_openai_tool(self) -> dict:
        desc = self.description
        if self.body.strip():
            desc = f"{desc}\n\n{self.body.strip()}"
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": desc,
                "parameters": self.parameters or {
                    "type": "object", "properties": {}
                },
            },
        }

    async def run(self, args: Dict[str, Any], ctx: Dict) -> Dict:
        executor = get_executor(self.executor_name)
        if not executor:
            return {"success": False,
                    "error": f"未知执行器: {self.executor_name}"}
        ctx = dict(ctx or {})
        ctx["_skill"] = {
            **self.extra,
            "executor_args": self.executor_args,
            "name": self.name,
        }
        return await executor(args, ctx)


_FRONT_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)


def _parse_md(path: Path) -> Optional[Skill]:
    text = path.read_text(encoding="utf-8")
    m = _FRONT_RE.match(text)
    if not m:
        raise ValueError(f"{path.name}: 缺少 YAML front-matter")
    meta = yaml.safe_load(m.group(1)) or {}
    body = m.group(2) or ""
    for key in ("name", "description", "executor"):
        if key not in meta:
            raise ValueError(f"{path.name}: 缺少字段 {key}")
    return Skill(meta, body, path)


class SkillRegistry:
    def __init__(self):
        self.skills: Dict[str, Skill] = {}

    def load_all(self):
        self.skills.clear()
        if not DEF_DIR.exists():
            log.warning(f"skills dir not found: {DEF_DIR}")
            return
        for f in sorted(DEF_DIR.glob("*.md")):
            try:
                skill = _parse_md(f)
                self.skills[skill.name] = skill
            except Exception as e:
                log.warning(f"skip skill {f.name}: {e}")
        log.info("skills loaded",
                 extra=log_extra(count=len(self.skills),
                                 names=list(self.skills.keys())))

    def tool_schemas(self) -> List[dict]:
        return [s.to_openai_tool() for s in self.skills.values()]

    def get(self, name: str) -> Optional[Skill]:
        return self.skills.get(name)


registry = SkillRegistry()
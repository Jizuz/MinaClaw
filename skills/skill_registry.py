from typing import Dict, List, Any
from skills.base_skill import BaseSkill

class SkillRegistry:
    def __init__(self):
        self.skills: Dict[str, BaseSkill] = {}

    def register(self, skill: BaseSkill):
        self.skills[skill.name] = skill

    def get_all_schemas(self) -> List[Dict[str, Any]]:
        return [s.schema for s in self.skills.values()]

    async def execute(self, skill_name: str, args: Dict[str, Any]) -> str:
        skill = self.skills.get(skill_name)
        if not skill:
            return f"Error: skill {skill_name} not found"
        try:
            return await skill.run(args)
        except Exception as e:
            return f"Skill exec error: {str(e)}"
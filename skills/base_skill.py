from abc import ABC, abstractmethod
from typing import Dict, Any

class BaseSkill(ABC):
    name: str
    description: str

    @property
    def schema(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters()
        }

    @abstractmethod
    def parameters(self) -> Dict[str, Any]:
        pass

    @abstractmethod
    async def run(self, args: Dict[str, Any]) -> str:
        pass
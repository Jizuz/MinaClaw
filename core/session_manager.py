import time
import uuid
from typing import List, Dict
from dataclasses import dataclass, field

from config import settings
from utils.token_counter import count_tokens


@dataclass
class Message:
    role: str
    content: str
    ts: float = field(default_factory=time.time)
    tokens: int = 0

    def __post_init__(self):
        if not self.tokens:
            self.tokens = count_tokens(self.content)


class SessionManager:
    """内存会话管理 + 上下文压缩"""

    def __init__(self):
        self.sessions: Dict[str, dict] = {}

    def create(self) -> str:
        sid = uuid.uuid4().hex[:12]
        self.sessions[sid] = {
            "id": sid,
            "created_at": time.time(),
            "history": [],
            "summary": "",
        }
        return sid

    def exists(self, sid: str) -> bool:
        return sid in self.sessions

    def append(self, sid: str, role: str, content: str):
        self.sessions[sid]["history"].append(
            Message(role=role, content=content))

    def get_context(self, sid: str, system_prompt: str) -> List[dict]:
        sess = self.sessions[sid]
        history: List[Message] = sess["history"]

        messages = [{"role": "system", "content": system_prompt}]
        if sess["summary"]:
            messages.append({"role": "system",
                             "content": f"[历史摘要] {sess['summary']}"})

        used = count_tokens(system_prompt) + count_tokens(sess["summary"])
        budget = settings.max_context_tokens - used

        selected: List[Message] = []
        for m in reversed(history):
            if m.tokens > budget:
                break
            selected.insert(0, m)
            budget -= m.tokens

        dropped_count = len(history) - len(selected)
        if dropped_count > 0:
            dropped = history[:dropped_count]
            sess["history"] = selected
            sess["summary"] = self._make_summary(dropped, sess["summary"])

        for m in selected:
            messages.append({"role": m.role, "content": m.content})

        return messages

    def _make_summary(self, msgs: List[Message], old_summary: str) -> str:
        chunks = []
        if old_summary:
            chunks.append(old_summary[:500])
        for m in msgs:
            text = m.content.replace("\n", " ")
            chunks.append(f"{m.role}: {text[:80]}")
        summary = " | ".join(chunks)
        return summary[-1200:] if len(summary) > 1200 else summary

    def list_sessions(self) -> List[dict]:
        return [
            {"id": s["id"], "created_at": s["created_at"],
             "message_count": len(s["history"])}
            for s in self.sessions.values()
        ]


session_manager = SessionManager()
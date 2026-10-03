"""
会话管理：内存缓存 + PostgreSQL 持久化

- 写穿（write-through）：create / append 同步落 sessions / messages 表
- 懒加载：get_context 首次访问时从库回填 history（重启后上下文恢复）
- 按用户隔离：create(user_id)、exists 校验归属（越权视为不存在）
- 冗余计数：sessions.message_count / total_tokens 随 append 维护
- 首条用户消息自动生成会话标题
"""
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List

from config import settings
from db import pool as db
from utils.logger import get_logger, log_extra
from utils.token_counter import count_tokens

log = get_logger("session")


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
    """内存缓存 + PostgreSQL 持久化（写穿、懒加载）"""

    def __init__(self):
        self.sessions: Dict[str, dict] = {}

    # ==================== 内部工具 ====================

    def _cache(self, sid: str, user_id: str, title: str, summary: str,
               created_at: float, loaded: bool) -> dict:
        sess = {
            "id": sid,
            "user_id": user_id,
            "created_at": created_at,
            "history": [],
            "summary": summary or "",
            "title": title or "",
            "loaded": loaded,
        }
        self.sessions[sid] = sess
        return sess

    async def _load_history(self, sid: str) -> dict:
        """从库回填会话与消息（保留内存中已有 history 不覆盖）"""
        row = await db.fetch_one(
            "SELECT id, user_id, title, summary, created_at "
            "FROM sessions WHERE id = %s", (sid,))
        if not row:
            raise KeyError(f"session not found: {sid}")

        sess = self.sessions.get(sid)
        if sess is None:
            sess = self._cache(
                sid, row["user_id"], row["title"] or "", row["summary"] or "",
                row["created_at"].timestamp(), False)

        if not sess["history"]:
            msgs = await db.fetch_all(
                "SELECT role, content, tokens, created_at FROM messages "
                "WHERE session_id = %s ORDER BY id", (sid,))
            sess["history"] = [
                Message(role=m["role"], content=m["content"],
                        ts=m["created_at"].timestamp(), tokens=m["tokens"])
                for m in msgs
            ]
        sess["loaded"] = True
        return sess

    # ==================== 对外接口 ====================

    async def create(self, user_id: str = "static", title: str = "") -> str:
        """创建会话（立即落库）"""
        sid = uuid.uuid4().hex[:12]
        self._cache(sid, user_id or "static", title, "", time.time(), True)
        await db.execute(
            "INSERT INTO sessions (id, user_id, title, summary) "
            "VALUES (%s, %s, %s, %s)",
            (sid, user_id or "static", title or None, ""))
        log.info("session created",
                 extra=log_extra(session_id=sid, user_id=user_id))
        return sid

    async def exists(self, sid: str, user_id: str = "") -> bool:
        """会话是否存在；user_id 非空时校验归属（越权视为不存在）"""
        sess = self.sessions.get(sid)
        if sess is not None:
            return (not user_id) or sess["user_id"] == user_id

        row = await db.fetch_one(
            "SELECT id, user_id, title, summary, created_at "
            "FROM sessions WHERE id = %s", (sid,))
        if not row:
            return False
        if user_id and row["user_id"] != user_id:
            return False
        self._cache(sid, row["user_id"], row["title"] or "",
                    row["summary"] or "", row["created_at"].timestamp(), False)
        return True

    async def append(self, sid: str, role: str, content: str) -> None:
        """追加消息：内存缓存 + messages 落库 + sessions 冗余计数/标题"""
        msg = Message(role=role, content=content)

        sess = self.sessions.get(sid)
        if sess is None:
            # 直接 append 到未知会话（理论上先经 exists/create）：尝试加载
            try:
                sess = await self._load_history(sid)
            except KeyError:
                log.warning("append to unknown session",
                            extra=log_extra(session_id=sid))
                return
        sess["history"].append(msg)

        # 首条用户消息自动生成标题（列表展示用）
        new_title = ""
        if role == "user" and not sess.get("title"):
            new_title = content.strip().replace("\n", " ")[:30]
            sess["title"] = new_title

        await db.execute(
            "INSERT INTO messages (session_id, role, content, tokens) "
            "VALUES (%s, %s, %s, %s)",
            (sid, role, content, msg.tokens))
        if new_title:
            await db.execute(
                "UPDATE sessions SET message_count = message_count + 1, "
                "total_tokens = total_tokens + %s, title = %s WHERE id = %s",
                (msg.tokens, new_title, sid))
        else:
            await db.execute(
                "UPDATE sessions SET message_count = message_count + 1, "
                "total_tokens = total_tokens + %s WHERE id = %s",
                (msg.tokens, sid))

    async def get_context(self, sid: str, system_prompt: str) -> List[dict]:
        """组装上下文：懒加载 history → 预算裁剪 → 必要时压缩摘要并写回库"""
        sess = self.sessions.get(sid)
        if sess is None or not sess.get("loaded"):
            sess = await self._load_history(sid)
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
            await db.execute(
                "UPDATE sessions SET summary = %s WHERE id = %s",
                (sess["summary"], sid))

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

    async def get_messages(self, sid: str, user_id: str = "") -> List[dict]:
        """读取会话消息（越权/不存在返回空列表）"""
        if user_id and not await self.exists(sid, user_id):
            return []
        rows = await db.fetch_all(
            "SELECT role, content, tokens, created_at FROM messages "
            "WHERE session_id = %s ORDER BY id", (sid,))
        return [
            {
                "role": r["role"],
                "content": r["content"],
                "tokens": r["tokens"],
                "ts": r["created_at"].timestamp(),
            }
            for r in rows
        ]

    async def list_sessions(self, user_id: str = "") -> List[dict]:
        """按用户列出会话（updated_at 倒序，含标题与统计）"""
        if user_id:
            rows = await db.fetch_all(
                "SELECT id, title, message_count, total_tokens, created_at "
                "FROM sessions WHERE user_id = %s AND archived = FALSE "
                "ORDER BY updated_at DESC LIMIT 200", (user_id,))
        else:
            rows = await db.fetch_all(
                "SELECT id, title, message_count, total_tokens, created_at "
                "FROM sessions WHERE archived = FALSE "
                "ORDER BY updated_at DESC LIMIT 200")
        return [
            {
                "id": r["id"],
                "title": r["title"] or r["id"],
                "message_count": r["message_count"],
                "total_tokens": r["total_tokens"],
                "created_at": r["created_at"].timestamp(),
            }
            for r in rows
        ]


session_manager = SessionManager()
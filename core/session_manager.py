"""
会话管理：内存缓存 + PostgreSQL 持久化

- 写穿（write-through）：create / append 同步落 sessions / messages 表
- 懒加载：get_context 首次访问时从库回填 history（重启后上下文恢复）
- 按用户隔离：create(user_id)、exists 校验归属（越权视为不存在）
- 冗余计数：sessions.message_count / total_tokens 随 append 维护
- 首条用户消息自动生成会话标题
"""
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List

from config import settings
from db import pool as db
from skills.skill_loader import registry
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


def _parse_loaded_skills(meta) -> List[dict]:
    """从 sessions.meta 恢复会话级技能加载记录（NULL / 缺键 / 脏数据兜底为空）"""
    if not isinstance(meta, dict):
        return []
    loaded = meta.get("loaded_skills")
    if not isinstance(loaded, list):
        return []
    return [
        {"name": r["name"], "gen": r["gen"]}
        for r in loaded
        if isinstance(r, dict) and "name" in r and "gen" in r
    ]


class SessionManager:
    """内存缓存 + PostgreSQL 持久化（写穿、懒加载）"""

    def __init__(self):
        self.sessions: Dict[str, dict] = {}

    # ==================== 内部工具 ====================

    def _cache(self, sid: str, user_id: str, title: str, summary: str,
               created_at: float, loaded: bool,
               loaded_skills: List[dict] | None = None) -> dict:
        sess = {
            "id": sid,
            "user_id": user_id,
            "created_at": created_at,
            "history": [],
            "summary": summary or "",
            "title": title or "",
            "loaded": loaded,
            # 会话级技能加载缓存：已加载技能记录（最近在后）+ 本轮已注入详情的技能名
            "loaded_skills": list(loaded_skills or []),
            "injected_skills": set(),
        }
        self.sessions[sid] = sess
        return sess

    async def _load_history(self, sid: str) -> dict:
        """从库回填会话与消息（保留内存中已有 history 不覆盖）"""
        row = await db.fetch_one(
            "SELECT id, user_id, title, summary, created_at, meta "
            "FROM sessions WHERE id = %s", (sid,))
        if not row:
            raise KeyError(f"session not found: {sid}")

        sess = self.sessions.get(sid)
        if sess is None:
            sess = self._cache(
                sid, row["user_id"], row["title"] or "", row["summary"] or "",
                row["created_at"].timestamp(), False,
                loaded_skills=_parse_loaded_skills(row.get("meta")))
        elif not sess.get("loaded_skills"):
            # 既有缓存但记录为空（exists() 等更早路径未带回）：自 meta 回填
            sess["loaded_skills"] = _parse_loaded_skills(row.get("meta"))

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
            "SELECT id, user_id, title, summary, created_at, meta "
            "FROM sessions WHERE id = %s", (sid,))
        if not row:
            return False
        if user_id and row["user_id"] != user_id:
            return False
        self._cache(sid, row["user_id"], row["title"] or "",
                    row["summary"] or "", row["created_at"].timestamp(), False,
                    loaded_skills=_parse_loaded_skills(row.get("meta")))
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

    # ==================== 会话级技能加载缓存 ====================

    async def _persist_loaded_skills(self, sid: str) -> None:
        """把 loaded_skills 写穿到 sessions.meta（失败仅记日志，不阻断对话）"""
        sess = self.sessions.get(sid)
        if sess is None:
            return
        try:
            await db.execute(
                "UPDATE sessions SET meta = jsonb_set("
                "COALESCE(meta, '{}'::jsonb), '{loaded_skills}', %s::jsonb) "
                "WHERE id = %s",
                (json.dumps(sess["loaded_skills"], ensure_ascii=False), sid))
        except Exception as e:
            log.warning("persist loaded_skills failed",
                        extra=log_extra(session_id=sid, error=str(e)))

    async def record_skill_load(self, sid: str, name: str, gen: int) -> None:
        """记录已加载技能（同名去重，最近性置顶）；内存更新 + 写穿 meta"""
        sess = self.sessions.get(sid)
        if sess is None:
            return
        loaded = [r for r in (sess.get("loaded_skills") or [])
                  if r.get("name") != name]
        loaded.append({"name": name, "gen": gen})
        sess["loaded_skills"] = loaded
        await self._persist_loaded_skills(sid)

    async def clear_skill_load(self, sid: str, name: str) -> None:
        """清除某个技能的加载记录（技能删除等失效场景）"""
        sess = self.sessions.get(sid)
        if sess is None:
            return
        old = sess.get("loaded_skills") or []
        new = [r for r in old if r.get("name") != name]
        if len(new) == len(old):
            return
        sess["loaded_skills"] = new
        await self._persist_loaded_skills(sid)

    async def _build_skill_injection(self, sess: dict) -> tuple[str, set]:
        """组装 [已加载技能] 轮首注入消息；返回 (消息文本, 实际注入详情的技能名集合)

        - 仅渐进式模式且 skill_session_cache 开启时注入，否则返回空
        - 记录有效性（D2）：技能仍注册 且 记录 gen == registry.generation；
          无效记录跳过注入并在内存中剔除（含写穿，已删除技能的记录随之清除）
        - 最近加载优先注入，详情总量受 skill_session_cache_max_chars 约束，
          超限技能仅列名提示可重新 load_skill 获取
        """
        if not (settings.skill_progressive and settings.skill_session_cache):
            return "", set()

        valid = []
        for r in sess.get("loaded_skills") or []:
            skill = registry.skills.get(r.get("name"))
            if skill is not None and r.get("gen") == registry.generation:
                valid.append(r)
        if len(valid) != len(sess.get("loaded_skills") or []):
            sess["loaded_skills"] = valid
            await self._persist_loaded_skills(sess["id"])

        sections = []   # [(name, "## name\ndetail")]，最近加载在前
        names_only = []
        budget = settings.skill_session_cache_max_chars
        for r in reversed(valid):   # loaded_skills 最近在后
            skill = registry.skills[r["name"]]
            detail = skill.detail_text()
            if len(detail) <= budget:
                sections.append((skill.name, f"## {skill.name}\n{detail}"))
                budget -= len(detail)
            else:
                names_only.append(skill.name)

        if not sections and not names_only:
            return "", set()

        parts = ["[已加载技能] 以下技能完整说明已在本会话加载并注入当前上下文，"
                 "可直接使用，无需再次调用 load_skill："]
        parts.extend(text for _, text in sections)
        if names_only:
            parts.append("（以下技能因注入长度限制未包含详情，需要时可重新调用 "
                         "load_skill 获取：" + "、".join(names_only) + "）")
        return "\n\n".join(parts), {name for name, _ in sections}

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

        # ---- 会话级技能加载缓存：轮首注入已加载技能详情（tokens 计入预算）----
        injected_msg, injected_names = await self._build_skill_injection(sess)
        sess["injected_skills"] = injected_names
        if injected_msg:
            messages.append({"role": "system", "content": injected_msg})
            used += count_tokens(injected_msg)

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
"""
Token 检测

- 使用 tiktoken 精确计算 prompt / completion
- 累积统计：全局 / 按 API Key / 按会话
- 每日配额 + 预警阈值
- 落盘：logs/tokens.jsonl（每次调用一行）
- 提供快照接口供 /api/tokens
"""
import time
import json
import threading
from dataclasses import dataclass, field, asdict
from pathlib import Path
from collections import defaultdict
from typing import Dict, Any, Tuple

import tiktoken

from config import settings
from utils.logger import get_logger, get_audit_logger, log_extra

log = get_logger("token_meter")
audit = get_audit_logger()

_enc = tiktoken.get_encoding("cl100k_base")
_lock = threading.Lock()

TOKEN_LOG = Path(settings.log_dir) / "tokens.jsonl"


def count_tokens(text: str) -> int:
    if not text:
        return 0
    try:
        return len(_enc.encode(text))
    except Exception:
        return max(1, len(text) // 2)


def count_messages_tokens(messages: list) -> int:
    """估算一组消息的 prompt token（含角色与结构开销）"""
    total = 0
    for m in messages:
        total += 4  # 每条消息的结构开销
        content = m.get("content") or ""
        total += count_tokens(content if isinstance(content, str)
                              else json.dumps(content, ensure_ascii=False))
        if m.get("role"):
            total += count_tokens(m["role"])
        for tc in m.get("tool_calls") or []:
            total += count_tokens(tc.get("function", {}).get("name", ""))
            total += count_tokens(
                tc.get("function", {}).get("arguments", ""))
    return total + 2


# ---------- 数据结构 ----------

@dataclass
class Usage:
    prompt: int = 0
    completion: int = 0
    total: int = 0
    calls: int = 0
    cost_units: float = 0.0   # 自定义计费单位，可选


@dataclass
class KeyUsage(Usage):
    day: str = field(default_factory=lambda: time.strftime("%Y-%m-%d"))


@dataclass
class TokenStats:
    started_at: float = field(default_factory=time.time)
    global_usage: Usage = field(default_factory=Usage)
    per_key: Dict[str, KeyUsage] = field(default_factory=dict)
    per_session: Dict[str, Usage] = field(default_factory=dict)
    hourly: Dict[str, Usage] = field(default_factory=dict)  # "YYYY-MM-DD HH"
    last_error: str = ""


class TokenMeter:
    def __init__(self):
        self.stats = TokenStats()

    # -------- 记录 --------
    def record(
        self,
        api_key: str,
        session_id: str,
        prompt: int,
        completion: int,
        model: str = "",
        kind: str = "chat",
    ) -> Tuple[Usage, bool]:
        """
        记录一次调用的 token 消耗。
        返回 (本次累计，是否超配额)
        """
        total = prompt + completion
        hour = time.strftime("%Y-%m-%d %H")
        day = time.strftime("%Y-%m-%d")

        with _lock:
            # 全局
            g = self.stats.global_usage
            g.prompt += prompt
            g.completion += completion
            g.total += total
            g.calls += 1

            # 每 Key
            ku = self.stats.per_key.get(api_key)
            if ku is None or ku.day != day:
                ku = KeyUsage(day=day)
                self.stats.per_key[api_key] = ku
            ku.prompt += prompt
            ku.completion += completion
            ku.total += total
            ku.calls += 1

            # 每会话
            su = self.stats.per_session.setdefault(session_id, Usage())
            su.prompt += prompt
            su.completion += completion
            su.total += total
            su.calls += 1

            # 每小时
            hu = self.stats.hourly.setdefault(hour, Usage())
            hu.prompt += prompt
            hu.completion += completion
            hu.total += total
            hu.calls += 1

            used_today = ku.total
            over_quota = used_today >= settings.daily_token_quota
            near_warn = (not over_quota
                         and used_today >= settings.daily_token_warn)

        # 日志
        log.info(
            f"token usage kind={kind} model={model} "
            f"prompt={prompt} completion={completion} total={total}",
            extra=log_extra(
                kind=kind, model=model,
                prompt=prompt, completion=completion, total=total,
                used_today=used_today,
                quota=settings.daily_token_quota,
            ),
        )

        # 审计日志
        audit.info(
            "token",
            extra=log_extra(
                kind=kind, model=model,
                prompt=prompt, completion=completion, total=total,
                api_key_masked=(api_key[:4] + "***"),
                session_id=session_id,
                used_today=used_today,
            ),
        )

        # 落盘
        try:
            with TOKEN_LOG.open("a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "ts": time.time(),
                    "kind": kind,
                    "model": model,
                    "api_key": api_key[:4] + "***",
                    "session_id": session_id,
                    "prompt": prompt,
                    "completion": completion,
                    "total": total,
                    "used_today": used_today,
                    "quota": settings.daily_token_quota,
                }, ensure_ascii=False) + "\n")
        except Exception as e:
            self.stats.last_error = str(e)

        if over_quota:
            log.warning(
                f"daily token quota exceeded key={api_key[:4]}*** "
                f"used={used_today} quota={settings.daily_token_quota}",
                extra=log_extra(used=used_today,
                                quota=settings.daily_token_quota),
            )
        elif near_warn:
            log.warning(
                f"daily token near warn "
                f"used={used_today} warn={settings.daily_token_warn}",
                extra=log_extra(used=used_today,
                                warn=settings.daily_token_warn),
            )

        return Usage(prompt=prompt, completion=completion, total=total,
                     calls=1), over_quota

    # -------- 查询 --------
    def check_quota(self, api_key: str) -> Tuple[bool, int, int]:
        """返回 (是否还有额度, 已用, 配额)"""
        day = time.strftime("%Y-%m-%d")
        with _lock:
            ku = self.stats.per_key.get(api_key)
            used = ku.total if ku and ku.day == day else 0
        return used < settings.daily_token_quota, used, settings.daily_token_quota

    def snapshot(self, api_key: str | None = None,
                 session_id: str | None = None) -> Dict[str, Any]:
        with _lock:
            g = asdict(self.stats.global_usage)
            top_keys = sorted(
                self.stats.per_key.items(),
                key=lambda kv: kv[1].total, reverse=True)[:10]
            top_sessions = sorted(
                self.stats.per_session.items(),
                key=lambda kv: kv[1].total, reverse=True)[:10]

            out: Dict[str, Any] = {
                "uptime_sec": round(time.time() - self.stats.started_at, 1),
                "global": g,
                "top_keys": [
                    {"key": k[:4] + "***", **asdict(u)}
                    for k, u in top_keys
                ],
                "top_sessions": [
                    {"session_id": s, **asdict(u)}
                    for s, u in top_sessions
                ],
                "hourly": {h: asdict(u) for h, u in
                           sorted(self.stats.hourly.items())[-24:]},
                "quota": settings.daily_token_quota,
                "warn": settings.daily_token_warn,
                "last_error": self.stats.last_error,
            }

            if api_key:
                ku = self.stats.per_key.get(api_key)
                out["this_key"] = asdict(ku) if ku else None
            if session_id:
                su = self.stats.per_session.get(session_id)
                out["this_session"] = asdict(su) if su else None

            return out


token_meter = TokenMeter()
"""
结构化日志

- JSON 行格式（每行一条）
- 通过 contextvars 注入 request_id / session_id / task_id / api_key
- 分级：DEBUG / INFO / WARNING / ERROR
- 同时输出到 stdout 与文件（按天滚动）
- 审计日志单独落盘（用户消息、工具调用、Token 消耗）
"""
import os
import sys
import json
import time
import logging
import contextvars
from pathlib import Path
from logging.handlers import TimedRotatingFileHandler
from typing import Any, Dict

from config import settings

LOG_DIR = Path(settings.log_dir)
LOG_DIR.mkdir(exist_ok=True)

# ---------- 上下文变量 ----------
ctx_request_id: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")
ctx_session_id: contextvars.ContextVar[str] = contextvars.ContextVar("session_id", default="-")
ctx_task_id: contextvars.ContextVar[str] = contextvars.ContextVar("task_id", default="-")
ctx_api_key: contextvars.ContextVar[str] = contextvars.ContextVar("api_key", default="-")


def bind_context(**kwargs):
    """绑定上下文，返回 token 列表用于 reset"""
    tokens = []
    if "request_id" in kwargs:
        tokens.append((ctx_request_id, ctx_request_id.set(kwargs["request_id"])))
    if "session_id" in kwargs:
        tokens.append((ctx_session_id, ctx_session_id.set(kwargs["session_id"])))
    if "task_id" in kwargs:
        tokens.append((ctx_task_id, ctx_task_id.set(kwargs["task_id"])))
    if "api_key" in kwargs:
        tokens.append((ctx_api_key, ctx_api_key.set(kwargs["api_key"])))
    return tokens


def reset_context(tokens):
    for var, tok in tokens:
        var.reset(tok)


# ---------- 脱敏 ----------
def _mask_key(k: str) -> str:
    if not k or len(k) < 6:
        return "***"
    return f"{k[:4]}***{k[-2:]}"


# ---------- Formatter ----------
class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data: Dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S",
                                time.localtime(record.created))
                  + f".{int(record.msecs):03d}",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": ctx_request_id.get(),
            "session_id": ctx_session_id.get(),
            "task_id": ctx_task_id.get(),
            "api_key": _mask_key(ctx_api_key.get()),
        }
        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)

        # 附加字段（通过 logger.info(..., extra={"extra": {...}}) 传入）
        extra = getattr(record, "extra_fields", None)
        if extra:
            data.update(extra)

        return json.dumps(data, ensure_ascii=False)


class PlainFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = (f"{time.strftime('%H:%M:%S', time.localtime(record.created))}"
                f".{int(record.msecs):03d} "
                f"[{record.levelname:<7}] "
                f"[req={ctx_request_id.get()} "
                f"sess={ctx_session_id.get()} "
                f"task={ctx_task_id.get()}] "
                f"{record.getMessage()}")
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


# ---------- 初始化 ----------
def _build_handler(filename: str, level: int) -> TimedRotatingFileHandler:
    h = TimedRotatingFileHandler(
        LOG_DIR / filename, when="midnight", backupCount=14,
        encoding="utf-8",
    )
    h.setLevel(level)
    h.setFormatter(JsonFormatter() if settings.log_json else PlainFormatter())
    return h


def setup_logging():
    root = logging.getLogger()
    root.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))

    # 清空默认 handler
    for h in list(root.handlers):
        root.removeHandler(h)

    # stdout
    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))
    sh.setFormatter(JsonFormatter() if settings.log_json else PlainFormatter())
    root.addHandler(sh)

    # app.log
    root.addHandler(_build_handler("app.log", logging.INFO))

    # error.log（只收 WARNING+）
    root.addHandler(_build_handler("error.log", logging.WARNING))

    # audit.log
    audit = logging.getLogger("audit")
    audit.setLevel(logging.INFO)
    audit.propagate = False
    audit.addHandler(_build_handler("audit.log", logging.INFO))
    audit.addHandler(sh)

    # 降低噪音
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def get_audit_logger() -> logging.Logger:
    return logging.getLogger("audit")


def log_extra(**kwargs) -> Dict[str, Any]:
    """便捷附加字段：logger.info(msg, extra=log_extra(x=1))"""
    return {"extra_fields": kwargs}
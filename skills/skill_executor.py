"""
通用执行器
"""
import asyncio
import shlex
import httpx
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header
from typing import Callable, Dict, Any

from utils.sandbox import sandbox
from config import settings
from utils.logger import get_logger, log_extra

log = get_logger("executor")

ALLOWED_COMMANDS = {
    "ls":    {"max_args": 3, "flags": {"-l", "-a", "-h", "-la", "-lh"}},
    "cat":   {"max_args": 2, "flags": set()},
    "head":  {"max_args": 3, "flags": {"-n"}},
    "tail":  {"max_args": 3, "flags": {"-n"}},
    "grep":  {"max_args": 5, "flags": {"-i", "-n", "-r"}},
    "find":  {"max_args": 5, "flags": {"-name", "-type"}},
    "wc":    {"max_args": 3, "flags": {"-l", "-w", "-c"}},
    "echo":  {"max_args": 10, "flags": set()},
    "pwd":   {"max_args": 0, "flags": set()},
    "date":  {"max_args": 1, "flags": set()},
    "mkdir": {"max_args": 2, "flags": {"-p"}},
}
DANGEROUS = set("|;&$`><(){}*?\\\n")


def _validate_bash(cmd: str):
    for ch in DANGEROUS:
        if ch in cmd:
            return False, f"包含危险字符: {ch!r}"
    try:
        parts = shlex.split(cmd)
    except ValueError as e:
        return False, f"命令解析失败: {e}"
    if not parts:
        return False, "空命令"
    name = parts[0]
    if name not in ALLOWED_COMMANDS:
        return False, f"命令 '{name}' 不在白名单内"
    conf = ALLOWED_COMMANDS[name]
    args = parts[1:]
    if len(args) > conf["max_args"]:
        return False, f"参数过多，最多 {conf['max_args']} 个"
    for a in args:
        if a.startswith("-") and a not in conf["flags"] and not a[1:].isdigit():
            return False, f"参数 '{a}' 不被允许"
    for a in args:
        if a.startswith("-"):
            continue
        if "/" in a or a.startswith("."):
            try:
                sandbox.resolve(a)
            except PermissionError:
                return False, f"路径 '{a}' 超出沙箱"
    return True, "ok"


async def exec_file_read(args, ctx):
    path = args.get("path", "")
    log.info("file_read", extra=log_extra(path=path))
    try:
        p = sandbox.check_read(path)
        content = p.read_text(encoding="utf-8", errors="replace")
        if len(content) > 20000:
            content = content[:20000] + "\n...[内容已截断]"
        return {"success": True, "output": content}
    except Exception as e:
        log.warning("file_read failed",
                    extra=log_extra(path=path, error=str(e)))
        return {"success": False, "error": str(e)}


async def exec_file_write(args, ctx):
    path = args.get("path", "")
    content = args.get("content", "")
    log.info("file_write",
             extra=log_extra(path=path, size=len(content)))
    try:
        sandbox.check_size(content)
        p = sandbox.check_write(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return {"success": True,
                "output": f"已写入 {len(content)} 字符到 {p.name}"}
    except Exception as e:
        log.warning("file_write failed",
                    extra=log_extra(path=path, error=str(e)))
        return {"success": False, "error": str(e)}


async def exec_file_list(args, ctx):
    path = args.get("path", ".")
    log.info("file_list", extra=log_extra(path=path))
    try:
        items = sandbox.list_dir(path)
        lines = [f"{'[D]' if i['is_dir'] else '[F]'} {i['name']}"
                 + (f"  ({i['size']}B)" if not i['is_dir'] else "")
                 for i in items]
        return {"success": True, "output": "\n".join(lines) or "（空目录）"}
    except Exception as e:
        log.warning("file_list failed",
                    extra=log_extra(path=path, error=str(e)))
        return {"success": False, "error": str(e)}


async def exec_bash(args, ctx):
    cmd = (args.get("command") or "").strip()
    ok, msg = _validate_bash(cmd)
    log.info("bash",
             extra=log_extra(command=cmd, allowed=ok))
    if not ok:
        return {"success": False, "error": f"命令被拒绝: {msg}"}
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(sandbox.base),
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=settings.bash_timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            return {"success": False, "error": "命令执行超时"}
        out = stdout.decode("utf-8", "replace")
        err = stderr.decode("utf-8", "replace")
        if proc.returncode == 0:
            return {"success": True, "output": out or "(无输出)"}
        return {"success": False,
                "error": f"exit={proc.returncode}\n{err or out}"}
    except Exception as e:
        log.warning("bash failed",
                    extra=log_extra(command=cmd, error=str(e)))
        return {"success": False, "error": str(e)}


async def exec_email(args, ctx):
    to = args.get("to", "")
    subject = args.get("subject", "")
    body = args.get("body", "")
    log.info("email", extra=log_extra(to=to, subject=subject,
                                      body_len=len(body)))
    if not settings.smtp_host:
        return {"success": True,
                "output": (f"[模拟发送] 收件人={to}, "
                           f"主题={subject}, "
                           f"正文长度={len(body)}\n"
                           f"(未配置 SMTP，仅演示)")}
    try:
        import aiosmtplib
        msg = MIMEMultipart()
        msg["From"] = settings.smtp_from
        msg["To"] = to
        msg["Subject"] = Header(subject, "utf-8")
        msg.attach(MIMEText(body, "html", "utf-8"))
        await aiosmtplib.send(
            msg,
            hostname=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_user or None,
            password=settings.smtp_pass or None,
            start_tls=(settings.smtp_port == 587),
            use_tls=(settings.smtp_port == 465),
        )
        return {"success": True, "output": f"邮件已发送至 {to}"}
    except Exception as e:
        log.warning("email failed", extra=log_extra(to=to, error=str(e)))
        return {"success": False, "error": str(e)}


async def exec_template(args, ctx):
    skill = ctx.get("_skill", {})
    template = skill.get("template", "")
    try:
        rendered = template.format(**args)
        return {"success": True, "output": rendered}
    except Exception as e:
        return {"success": False, "error": f"模板渲染失败: {e}"}


async def exec_http(args, ctx):
    skill = ctx.get("_skill", {})
    cfg = skill.get("executor_args", {}) or {}
    method = cfg.get("method", "GET").upper()
    url_tpl = cfg.get("url", "")
    try:
        url = url_tpl.format(**args)
    except Exception as e:
        return {"success": False, "error": f"URL 模板渲染失败: {e}"}
    log.info("http", extra=log_extra(method=method, url=url))
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            if method == "GET":
                r = await client.get(url, params=cfg.get("params"))
            else:
                r = await client.request(method, url, json=args)
            return {"success": r.status_code < 400, "output": r.text[:5000]}
    except Exception as e:
        return {"success": False, "error": str(e)}


EXECUTORS: Dict[str, Callable] = {
    "file_read": exec_file_read,
    "file_write": exec_file_write,
    "file_list": exec_file_list,
    "bash": exec_bash,
    "email": exec_email,
    "template": exec_template,
    "http": exec_http,
}


def get_executor(name: str):
    return EXECUTORS.get(name)
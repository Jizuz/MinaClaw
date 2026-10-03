"""
技能包上传（zip 目录包，对齐 Agent Skills 开放标准）

上传流程：staging 暂存 -> 安全解压 -> 定位 SKILL.md -> 规范校验 ->
落位 defs/<name>/ -> 由路由层触发热重载生效；任何失败均回滚并清理暂存。

安全防护：zip-slip 路径穿越、zip bomb（条目数/解压总量/压缩包大小）、
附属文件扩展名白名单、隐藏条目跳过、executor 注册表校验、元工具名保护。
"""
import shutil
import tempfile
import zipfile
from pathlib import Path

from config import settings
from skills.skill_executor import EXECUTORS, get_executor
from skills.skill_loader import (
    DEF_DIR,
    META_TOOL_NAME,
    NAME_MAX_LEN,
    NAME_RE,
    _parse_md,
)
from utils.logger import get_logger, log_extra

log = get_logger("skills.upload")

# 暂存目录（隐藏目录，registry 的 */SKILL.md 扫描不会命中；正常路径必清理）
STAGING_DIR = DEF_DIR / ".uploads-tmp"

# 附属文件扩展名白名单（技能正文/参考资源，均为纯文本类）
ALLOWED_EXTS = {".md", ".txt", ".json", ".csv", ".yaml", ".yml",
                ".html", ".css", ".js", ".py", ".log"}


class UploadError(Exception):
    """上传/删除失败（status_code 映射 HTTP 状态）"""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def _check_entry_name(name: str):
    """拒绝绝对路径、盘符与 .. 条目名（zip-slip 防护）"""
    norm = name.replace("\\", "/")
    if norm.startswith("/") or (len(norm) > 1 and norm[1] == ":"):
        raise UploadError(f"非法路径条目: '{name}'（不允许绝对路径）")
    if any(p == ".." for p in norm.split("/")):
        raise UploadError(f"非法路径条目: '{name}'（不允许 ..）")


def _copy_limited(src, out, limit: int, label: str) -> int:
    """限量拷贝：即使条目声明的 file_size 谎报，实际写出也绝不超限"""
    written = 0
    while True:
        chunk = src.read(64 * 1024)
        if not chunk:
            break
        written += len(chunk)
        if written > limit:
            raise UploadError(f"{label} 解压超限", 413)
        out.write(chunk)
    return written


def _safe_extract(zip_path: Path, dest: Path) -> int:
    """安全解压：路径穿越/条目数/累计大小/扩展名/隐藏条目全部防护"""
    max_files = settings.skill_upload_max_files
    max_total = settings.skill_upload_max_total_mb * 1024 * 1024
    dest.mkdir(parents=True, exist_ok=True)
    files = total = 0
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            # 先做路径穿越检查（.. / 绝对路径属明确恶意信号，直接拒绝）
            _check_entry_name(info.filename)
            parts = [p for p in info.filename.replace("\\", "/").split("/") if p]
            # 跳过隐藏/系统条目（.DS_Store、__MACOSX 等）
            if not parts or any(p.startswith(".") or p == "__MACOSX" for p in parts):
                continue
            files += 1
            if files > max_files:
                raise UploadError(f"包内文件数超过 {max_files} 个", 413)
            ext = Path(parts[-1]).suffix.lower()
            if ext not in ALLOWED_EXTS:
                raise UploadError(
                    f"不允许的文件类型: '{info.filename}'"
                    f"（白名单: {', '.join(sorted(ALLOWED_EXTS))}）")
            target = dest.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                total += _copy_limited(
                    src, out, max_total - total, f"'{info.filename}'")
    if files == 0:
        raise UploadError("包内没有可用的文件条目")
    return total


def _locate_skill_md(extract_dir: Path) -> Path:
    """定位包内唯一的 SKILL.md"""
    candidates = sorted(extract_dir.rglob("SKILL.md"))
    if not candidates:
        raise UploadError("包内缺少 SKILL.md（技能主文件）")
    if len(candidates) > 1:
        raise UploadError("包内包含多个 SKILL.md，一次仅可上传一个技能")
    return candidates[0]


def upload_skill(zip_bytes: bytes, overwrite: bool = False) -> dict:
    """校验并落位一个技能目录包，返回技能摘要；失败抛 UploadError"""
    if not settings.skill_upload_enabled:
        raise UploadError("技能上传已禁用（SKILL_UPLOAD_ENABLED=false）", 503)
    max_zip = settings.skill_upload_max_zip_mb * 1024 * 1024
    if len(zip_bytes) > max_zip:
        raise UploadError(
            f"压缩包超过 {settings.skill_upload_max_zip_mb}MB 限制", 413)

    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=STAGING_DIR))
    try:
        zip_path = staging / "upload.zip"
        zip_path.write_bytes(zip_bytes)
        try:
            with zipfile.ZipFile(zip_path) as zf:
                if zf.testzip() is not None:
                    raise UploadError("压缩包损坏（存在校验失败的条目）")
        except zipfile.BadZipFile:
            raise UploadError("文件不是有效的 zip 压缩包")

        extract_dir = staging / "extracted"
        _safe_extract(zip_path, extract_dir)

        skill_md = _locate_skill_md(extract_dir)
        rel = skill_md.relative_to(extract_dir)
        if len(rel.parts) == 1:            # 布局 A：打包的是技能目录内容
            skill_root = extract_dir
        elif len(rel.parts) == 2:          # 布局 B：打包的是技能目录本身
            dir_name = rel.parts[0]
            if not NAME_RE.match(dir_name) or len(dir_name) > NAME_MAX_LEN:
                raise UploadError(
                    f"技能目录名 '{dir_name}' 不符合规范"
                    f"（小写字母/数字/连字符，≤{NAME_MAX_LEN}）")
            skill_root = skill_md.parent
        else:
            raise UploadError(
                "SKILL.md 层级过深：包内应为技能目录内容（根级 SKILL.md）"
                "或技能目录本身（<name>/SKILL.md）")

        # ---- 预校验（规范 + 执行器 + 保留名）----
        try:
            preview = _parse_md(skill_md)
        except Exception as e:
            raise UploadError(f"技能定义校验失败: {e}")
        if preview.name == META_TOOL_NAME:
            raise UploadError(
                f"技能名 '{META_TOOL_NAME}' 为系统保留（内置元工具）")
        if skill_root != extract_dir and rel.parts[0] != preview.name:
            raise UploadError(
                f"name '{preview.name}' 与包内目录名 '{rel.parts[0]}' 不一致")
        if preview.executor_name == "skill_loader":
            raise UploadError(
                "执行器 'skill_loader' 为元工具专用，不可用于上传技能")
        if not get_executor(preview.executor_name):
            raise UploadError(
                f"未知执行器: '{preview.executor_name}'"
                f"（可用: {', '.join(sorted(k for k in EXECUTORS if k != 'skill_loader'))}）")

        # ---- 落位（冲突覆盖 + 失败回滚）----
        target = DEF_DIR / preview.name
        backup = None
        if target.exists():
            if not overwrite:
                raise UploadError(
                    f"技能 '{preview.name}' 已存在（可传 overwrite=true 覆盖）", 409)
            backup = staging / "backup"
            target.rename(backup)
        try:
            shutil.copytree(skill_root, target)
            try:
                # 权威校验：带 dir_name（name 与目录一致），附属文件自此生效
                skill = _parse_md(target / "SKILL.md", dir_name=preview.name)
            except Exception as e:
                raise UploadError(f"技能定义校验失败: {e}")
        except Exception:
            shutil.rmtree(target, ignore_errors=True)
            if backup is not None:
                backup.rename(target)
            raise
        assets = [str(p.relative_to(target)) for p in skill.assets]
        log.info("skill uploaded",
                 extra=log_extra(name=skill.name, executor=skill.executor_name,
                                  overwritten=backup is not None, assets=assets))
        return {
            "name": skill.name,
            "description": skill.description,
            "executor": skill.executor_name,
            "assets": assets,
            "overwritten": backup is not None,
        }
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def delete_skill(name: str) -> dict:
    """删除技能（目录形式优先，兼容旧单文件形式）；失败抛 UploadError"""
    if name == META_TOOL_NAME:
        raise UploadError(f"内置元工具 '{META_TOOL_NAME}' 不可删除")
    if not NAME_RE.match(name) or len(name) > NAME_MAX_LEN:
        raise UploadError(f"非法技能名: '{name}'")
    removed = []
    target_dir = DEF_DIR / name
    target_file = DEF_DIR / f"{name}.md"
    if target_dir.is_dir():
        shutil.rmtree(target_dir)
        removed.append(f"{name}/")
    if target_file.is_file():
        target_file.unlink()
        removed.append(f"{name}.md")
    if not removed:
        raise UploadError(f"技能不存在: '{name}'", 404)
    log.info("skill deleted", extra=log_extra(name=name, removed=removed))
    return {"name": name, "removed": removed}


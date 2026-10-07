import re
from pathlib import Path
from typing import Dict, Any, List, Optional
import yaml

from skills.skill_executor import get_executor
from utils.logger import get_logger, log_extra

log = get_logger("skills")

DEF_DIR = Path(__file__).parent / "defs"

# ---- Agent Skills 开放标准约束 ----
NAME_RE = re.compile(r"^[a-z0-9-]+$")
NAME_MAX_LEN = 64
DESC_MAX_LEN = 1024
ASSETS_MAX_CHARS = 20000  # load_skill 内联附属文件的详情上限

# ---- 渐进式加载：内置元工具 ----
META_TOOL_NAME = "load_skill"
META_TOOL_DESC = (
    "加载某个技能的完整使用说明（渐进式加载）。"
    "工具列表中的描述仅为摘要；在首次调用一个技能之前，"
    "如需了解其详细用法、限制、示例与捆绑的参考文件，"
    "先调用本工具获取完整定义，再按说明调用目标技能。"
    "系统会把本会话已加载技能的完整说明以 [已加载技能] 系统消息注入上下文，"
    "已注入的技能无需重复调用本工具。"
)


class Skill:
    def __init__(self, meta: Dict[str, Any], body: str, source: Path,
                 assets: Optional[List[Path]] = None):
        self.meta = meta
        self.body = body
        self.source = source
        # 附属文件（Agent Skills Level-3 资源，仅目录形式技能支持）
        self.assets: List[Path] = assets or []

        self.name: str = meta["name"]
        self.description: str = meta["description"]
        self.parameters: Dict = meta.get("parameters", {})
        self.executor_name: str = meta["executor"]
        self.executor_args: Dict = meta.get("executor_args", {}) or {}
        self.extra = {k: v for k, v in meta.items()
                      if k not in ("name", "description",
                                   "parameters", "executor", "executor_args")}

    def detail_text(self) -> str:
        """完整定义：description + 正文 + 附属文件内联（Level 2/3 披露）"""
        desc = self.description
        if self.body.strip():
            desc = f"{desc}\n\n{self.body.strip()}"
        base_dir = self.source.parent
        for p in self.assets:
            try:
                content = p.read_text(encoding="utf-8", errors="replace")
            except Exception as e:
                log.warning(f"asset read failed: {p.name}: {e}")
                continue
            rel = p.relative_to(base_dir)
            desc += f"\n\n--- file: {rel} ---\n{content.rstrip()}"
        if len(desc) > ASSETS_MAX_CHARS:
            desc = desc[:ASSETS_MAX_CHARS] + "\n...[详情已截断]"
        return desc

    def to_openai_tool(self, compact: bool = False) -> dict:
        """compact=True 时仅注入摘要（不含正文），配合 load_skill 渐进式加载"""
        desc = self.description if compact else self.detail_text()
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
_CONFIG_BLOCK_RE = re.compile(r"```yaml[ \t]*\r?\n(.*?)```", re.DOTALL)


def _extract_config(body: str) -> tuple:
    """提取正文中的 MinaClaw 扩展配置块（首个含 executor 键的 yaml 围栏块）

    返回 (config, 清理后的正文)。配置块会从正文中剔除，
    不进入给 LLM 的说明文字。
    """
    for m in _CONFIG_BLOCK_RE.finditer(body):
        try:
            data = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError:
            continue
        if isinstance(data, dict) and "executor" in data:
            cleaned = body[:m.start()] + body[m.end():]
            return data, cleaned
    return {}, body


def _find_assets(base_dir: Path) -> List[Path]:
    """目录形式技能的附属文件（Agent Skills Level-3 资源）

    递归收集 SKILL.md 同目录下的文件，跳过隐藏文件/目录。
    """
    assets: List[Path] = []
    for p in sorted(base_dir.rglob("*")):
        if not p.is_file() or p.name == "SKILL.md":
            continue
        rel = p.relative_to(base_dir)
        if any(part.startswith(".") for part in rel.parts):
            continue
        assets.append(p)
    return assets


def _parse_md(path: Path, dir_name: Optional[str] = None) -> Skill:
    """解析技能定义（Agent Skills 开放标准 + MinaClaw 扩展）

    - front-matter 仅认标准字段：name（^[a-z0-9-]+$，≤64，目录形式须与
      目录名一致）+ description（≤1024）+ 可选 license/metadata/allowed-tools
    - executor/parameters 等 MinaClaw 扩展配置优先取正文 yaml 配置块；
      兼容旧格式（front-matter 直接声明），front-matter 优先
    - dir_name 非空表示目录形式（defs/<name>/SKILL.md），附属文件生效
    """
    text = path.read_text(encoding="utf-8")
    m = _FRONT_RE.match(text)
    if not m:
        raise ValueError(f"{path.name}: 缺少 YAML front-matter")
    meta = yaml.safe_load(m.group(1)) or {}
    body = m.group(2) or ""

    # ---- 开放标准校验 ----
    name = meta.get("name")
    if not name or not isinstance(name, str):
        raise ValueError(f"{path.name}: 缺少字段 name")
    if not NAME_RE.match(name) or len(name) > NAME_MAX_LEN:
        raise ValueError(f"{path.name}: name '{name}' 不符合 Agent Skills "
                         f"规范（小写字母/数字/连字符，≤{NAME_MAX_LEN}）")
    if dir_name is not None and name != dir_name:
        raise ValueError(f"{path.name}: name '{name}' 与目录名 '{dir_name}' 不一致")
    desc = meta.get("description")
    if not desc or not isinstance(desc, str):
        raise ValueError(f"{path.name}: 缺少字段 description")
    if len(desc) > DESC_MAX_LEN:
        raise ValueError(f"{path.name}: description 超过 {DESC_MAX_LEN} 字符")

    # ---- MinaClaw 扩展配置 ----
    cfg, body = _extract_config(body)
    for key in ("executor", "parameters", "executor_args"):
        if key in meta:
            if key in cfg:
                log.warning(f"{path.name}: '{key}' 同时出现在 front-matter 与"
                            f"正文配置块，以 front-matter 为准")
            else:
                log.warning(f"{path.name}: '{key}' 位于 front-matter 为旧格式，"
                            f"建议迁移到正文 yaml 配置块")
            cfg[key] = meta.pop(key)
    if "executor" not in cfg:
        raise ValueError(f"{path.name}: 缺少 executor"
                         f"（正文 yaml 配置块或 front-matter）")
    meta.update(cfg)

    assets = _find_assets(path.parent) if dir_name is not None else []
    return Skill(meta, body, path, assets=assets)


class SkillRegistry:
    def __init__(self):
        self.skills: Dict[str, Skill] = {}
        # 世代计数：load_all() 每次调用自增（上传/删除/热重载统一经由 load_all），
        # 会话级加载缓存以「记录 gen == 当前世代」判定记录是否仍然有效
        self.generation: int = 0
        # 渐进式加载内置元工具（不进入 self.skills，不受热重载影响）
        self._meta_skill = Skill(
            meta={
                "name": META_TOOL_NAME,
                "description": META_TOOL_DESC,
                "executor": "skill_loader",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "skill": {
                            "type": "string",
                            "description": "目标技能的 name（工具函数名）",
                        }
                    },
                    "required": ["skill"],
                },
            },
            body="",
            source=Path(__file__),
        )

    def load_all(self):
        # 任何一次（重）加载都改变世代，使既有会话加载记录失效
        self.generation += 1
        self.skills.clear()
        if not DEF_DIR.exists():
            log.warning(f"skills dir not found: {DEF_DIR}")
            return
        # 目录形式（Agent Skills 开放标准，优先）：defs/<skill-name>/SKILL.md
        for f in sorted(DEF_DIR.glob("*/SKILL.md")):
            try:
                skill = _parse_md(f, dir_name=f.parent.name)
                if skill.name in self.skills:
                    log.warning(f"skip skill {skill.name}: 重复定义（{f}）")
                    continue
                self.skills[skill.name] = skill
            except Exception as e:
                log.warning(f"skip skill {f.parent.name}: {e}")
        # 单文件形式（兼容旧格式）：defs/*.md
        for f in sorted(DEF_DIR.glob("*.md")):
            try:
                skill = _parse_md(f)
                if skill.name in self.skills:
                    log.warning(f"skip skill {f.stem}: 已由目录形式定义，忽略单文件")
                    continue
                self.skills[skill.name] = skill
            except Exception as e:
                log.warning(f"skip skill {f.name}: {e}")
        log.info("skills loaded",
                 extra=log_extra(count=len(self.skills),
                                 names=list(self.skills.keys()),
                                 generation=self.generation))

    def tool_schemas(self, compact: bool = False) -> List[dict]:
        """compact=True 时仅注入技能摘要 + load_skill 元工具（渐进式加载）"""
        if compact and META_TOOL_NAME in self.skills:
            log.warning(f"skill '{META_TOOL_NAME}' 与内置元工具重名，回退全量注入")
            compact = False
        schemas: List[dict] = []
        if compact:
            schemas.append(self._meta_skill.to_openai_tool())
        schemas.extend(s.to_openai_tool(compact=compact)
                       for s in self.skills.values())
        return schemas

    def get(self, name: str) -> Optional[Skill]:
        if name == META_TOOL_NAME:
            return self._meta_skill
        return self.skills.get(name)


registry = SkillRegistry()
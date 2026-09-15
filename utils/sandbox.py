from pathlib import Path
from typing import List
from config import settings


class FileSandbox:
    def __init__(self, base_dir: str):
        self.base = Path(base_dir).resolve()
        self.base.mkdir(parents=True, exist_ok=True)
        self.allowed_ext = {
            ".txt", ".md", ".json", ".csv", ".py", ".js",
            ".html", ".css", ".yaml", ".yml", ".log"
        }

    def resolve(self, path: str) -> Path:
        p = Path(path)
        if not p.is_absolute():
            p = self.base / p
        p = p.resolve()
        try:
            p.relative_to(self.base)
        except ValueError:
            raise PermissionError(f"路径超出沙箱范围: {path}")
        if p.is_symlink():
            real = p.resolve()
            try:
                real.relative_to(self.base)
            except ValueError:
                raise PermissionError(f"符号链接指向沙箱外: {path}")
        return p

    def check_read(self, path: str) -> Path:
        p = self.resolve(path)
        if not p.exists():
            raise FileNotFoundError(f"文件不存在: {path}")
        if p.is_file() and p.suffix.lower() not in self.allowed_ext:
            raise PermissionError(f"不允许读取的文件类型: {p.suffix}")
        return p

    def check_write(self, path: str) -> Path:
        p = self.resolve(path)
        if p.suffix.lower() not in self.allowed_ext:
            raise PermissionError(f"不允许写入的文件类型: {p.suffix}")
        return p

    def check_size(self, content: str):
        size = len(content.encode("utf-8"))
        if size > settings.max_file_size_mb * 1024 * 1024:
            raise ValueError(f"内容超过 {settings.max_file_size_mb}MB 限制")

    def list_dir(self, subdir: str = ".") -> List[dict]:
        d = self.resolve(subdir)
        if not d.is_dir():
            return []
        out = []
        for item in sorted(d.iterdir()):
            out.append({
                "name": item.name,
                "is_dir": item.is_dir(),
                "size": item.stat().st_size if item.is_file() else 0,
            })
        return out


sandbox = FileSandbox(settings.sandbox_dir)
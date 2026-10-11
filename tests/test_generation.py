"""SkillRegistry.generation 世代计数验证（openspec: session-skill-load-cache 任务 1.2）

独立脚本直接运行：./claw/bin/python3 tests/test_generation.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from skills.skill_loader import registry  # noqa: E402


def main():
    # 全新进程：未调用 load_all() 前初值应为 0
    assert registry.generation == 0, \
        f"初值应为 0，实际 {registry.generation}"

    registry.load_all()
    first = registry.generation
    assert first == 1, f"第一次 load_all() 后应为 1，实际 {first}"

    registry.load_all()
    second = registry.generation
    assert second == first + 1, \
        f"连续第二次 load_all() 后应继续自增，{first} -> {second}"

    print(f"test_generation ok: 0 -> {first} -> {second}")


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""把 ipv6sync 源码打成 zip（供分发/归档）。

用法：
    python offline/pack_src.py            # 产出 dist/ipv6sync-<版本>-src.zip
    python offline/pack_src.py <输出目录>

收录：app/、tests/、logo/、根目录配置与文档、offline/ 下的构建脚本。
排除：offline/cache（基础镜像缓存）、offline/dist（镜像产物）、
      offline/boot-run-*（行为校验复现目录）、__pycache__ / *.pyc、
      *.log / *.tmp 以及以下划线开头的临时脚本。
"""
from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = "1.0.8"
try:                                     # 版本号跟 app/__init__.py 保持一致
    m = re.search(r'__version__\s*=\s*"([^"]+)"',
                  (ROOT / "app" / "__init__.py").read_text(encoding="utf-8"))
    if m:
        VERSION = m.group(1)
except OSError:
    pass

INCLUDE_FILES = [
    ".dockerignore", ".env.example", ".gitignore",
    "docker-compose.yml", "Dockerfile", "preview_console.py", "README.md",
]
INCLUDE_DIRS = ["app", "tests", "logo"]
OFFLINE_FILES = [
    "build.ps1", "build_offline.py", "validate_boot.py", "validate_image.py",
    "fetch_base.py", "pack_src.py", "README.md", "docker-compose.offline.yml",
]
SKIP_DIR_NAMES = {"__pycache__", "cache", "dist", ".git", ".venv", "node_modules"}
SKIP_SUFFIX = {".pyc", ".pyo", ".log", ".tmp"}


def keep(p: Path) -> bool:
    if any(part in SKIP_DIR_NAMES for part in p.parts):
        return False
    if p.suffix.lower() in SKIP_SUFFIX:
        return False
    # 下划线开头的临时脚本/日志不进包；但 __init__.py 是包结构，必须保留
    if p.name.startswith("_") and p.name != "__init__.py":
        return False
    return True


def collect() -> list[Path]:
    files: list[Path] = []
    for rel in INCLUDE_FILES:
        p = ROOT / rel
        if p.is_file():
            files.append(p)
    for d in INCLUDE_DIRS:
        base = ROOT / d
        if base.is_dir():
            files += [p for p in sorted(base.rglob("*")) if p.is_file() and keep(p)]
    for rel in OFFLINE_FILES:
        p = ROOT / "offline" / rel
        if p.is_file():
            files.append(p)
    return files


def main(argv: list[str]) -> int:
    out_dir = Path(argv[1]).resolve() if len(argv) > 1 else ROOT / "dist"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"ipv6sync-{VERSION}-src.zip"

    files = collect()
    arc_root = f"ipv6sync-{VERSION}"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for p in files:
            z.write(p, arc_root + "/" + p.relative_to(ROOT).as_posix())

    with zipfile.ZipFile(out) as z:
        bad = z.testzip()
        count = len(z.namelist())
    print(f"源码包: {out}")
    print(f"文件 {len(files)} 个 / 条目 {count} 条 / "
          f"{out.stat().st_size / 1024:.1f} KB")
    print("完整性: " + ("OK" if bad is None else f"损坏 {bad}"))
    return 0 if bad is None else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

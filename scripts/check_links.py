#!/usr/bin/env python3
"""校验仓库内 Markdown 的相对链接，防止文档重构后出现坏链。

只检查本地文件链接，跳过 http(s)、纯锚点、mailto。
用法: python scripts/check_links.py [--root <仓库根目录>]
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from urllib.parse import unquote

LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
SKIP_PREFIX = ("http://", "https://", "#", "mailto:", "data:")


def links_in(md: Path) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for lineno, line in enumerate(md.read_text(encoding="utf-8").splitlines(), 1):
        for raw in LINK_RE.findall(line):
            target = raw.strip()
            if target.startswith("<") and target.endswith(">"):
                target = target[1:-1].strip()
            # 去掉标题里的空格参数（如 `README.md "标题"`）
            target = target.split()[0] if target else ""
            if not target or target.startswith(SKIP_PREFIX):
                continue
            path_part = target.split("#", 1)[0]
            if not path_part:
                continue
            out.append((lineno, unquote(path_part)))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".", help="仓库根目录")
    args = ap.parse_args()
    root = Path(args.root).resolve()

    files = sorted(
        p for p in root.rglob("*.md")
        if not any(part in {".git", ".venv", "dist", "data", "node_modules"}
                   for part in p.parts)
    )
    broken: list[str] = []
    checked = 0

    for md in files:
        for lineno, target in links_in(md):
            checked += 1
            resolved = (md.parent / target).resolve()
            if not resolved.exists():
                broken.append(f"{md.relative_to(root)}:{lineno} -> {target}")

    print(f"检查 {len(files)} 个 Markdown 文件、{checked} 条本地链接")
    if broken:
        print(f"发现 {len(broken)} 条坏链:", file=sys.stderr)
        for item in broken:
            print("  " + item, file=sys.stderr)
        return 1
    print("全部有效 ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

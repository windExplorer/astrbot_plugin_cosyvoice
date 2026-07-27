#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打包 AstrBot 插件为 zip，用于上传到 AstrBot 后台「本地插件 / 从文件安装」。

用法：
    python pack.py
    python pack.py --output dist/astrbot_plugin_cosyvoice.zip

说明：
    - 以本脚本所在目录为插件根，整体压缩，zip 顶层目录名即插件名
      （astrbot_plugin_cosyvoice/），AstrBot 解压后即可识别。
    - 自动排除：.git、__pycache__、*.pyc、.venv、node_modules、dist、
      打包脚本自身，以及 deploy/api-优化（历史优化副本）等无需上线的内容。
"""

import argparse
import os
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
PLUGIN_NAME = os.path.basename(ROOT)

EXCLUDE_DIRS = {".git", "__pycache__", ".venv", "node_modules", "dist"}
EXCLUDE_FILES = {"pack.py", "pack.sh", "pack.bat"}
EXCLUDE_SUFFIXES = (".pyc", ".pyo")


def _should_exclude(rel_path: str) -> bool:
    parts = rel_path.split(os.sep)
    if parts[0] in EXCLUDE_DIRS:
        return True
    if rel_path in EXCLUDE_FILES:
        return True
    if any(p.endswith(EXCLUDE_SUFFIXES) for p in parts):
        return True
    return False


def main():
    parser = argparse.ArgumentParser(description="打包 AstrBot 插件为 zip")
    parser.add_argument(
        "--output",
        default=os.path.join(ROOT, "dist", PLUGIN_NAME + ".zip"),
        help="输出 zip 路径（默认 dist/<插件名>.zip）",
    )
    args = parser.parse_args()

    out = os.path.abspath(args.output)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    if os.path.exists(out):
        os.remove(out)

    count = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for base, dirs, files in os.walk(ROOT):
            # 不进入需排除的目录
            dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
            for f in files:
                fp = os.path.join(base, f)
                rel = os.path.relpath(fp, ROOT)
                if _should_exclude(rel):
                    continue
                arcname = os.path.join(PLUGIN_NAME, rel)
                z.write(fp, arcname)
                count += 1

    print(f"[pack] 已打包 {count} 个文件 -> {out}")


if __name__ == "__main__":
    main()

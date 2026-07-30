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
    - 默认产物带版本号：dist/<插件名>_v<版本号>.zip（版本取自 metadata.yaml）。
      若同名文件已存在则追加时间戳，不覆盖历史打包文件；可用 --output 指定路径覆盖。
"""

import argparse
import os
import re
import time
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
PLUGIN_NAME = os.path.basename(ROOT)

EXCLUDE_DIRS = {".git", "__pycache__", ".venv", "node_modules", "dist"}
EXCLUDE_FILES = {"pack.py", "pack.sh", "pack.bat"}
EXCLUDE_SUFFIXES = (".pyc", ".pyo")


def _read_version() -> str:
    """从 metadata.yaml 读取 version（去掉前缀 v/V）。读取失败回退 0.0.0。"""
    p = os.path.join(ROOT, "metadata.yaml")
    try:
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                m = re.match(r"^\s*version\s*:\s*(\S+)", line)
                if m:
                    return m.group(1).strip().lstrip("vV") or "0.0.0"
    except Exception:
        pass
    return "0.0.0"


def _unique_path(path: str) -> str:
    """若 path 已存在，追加时间戳返回新路径，避免覆盖历史文件。"""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    return f"{stem}_{time.strftime('%Y%m%d_%H%M%S')}{ext}"


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
        default=None,
        help="输出 zip 路径（默认 dist/<插件名>_v<版本号>.zip，已存在则加时间戳避免覆盖）",
    )
    args = parser.parse_args()

    if args.output:
        # 手动指定输出：保持原行为（覆盖已存在文件）
        out = os.path.abspath(args.output)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        if os.path.exists(out):
            os.remove(out)
    else:
        # 默认：文件名带版本号，且不覆盖已打好的包（同名则追加时间戳）
        version = _read_version()
        base = os.path.join(ROOT, "dist", f"{PLUGIN_NAME}_v{version}.zip")
        os.makedirs(os.path.dirname(base), exist_ok=True)
        out = _unique_path(base)

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

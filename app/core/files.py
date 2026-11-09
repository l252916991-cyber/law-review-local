"""Filesystem helpers: safe file names, containment checks and content digests."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path


def safe_filename(name: str, max_len: int = 180) -> str:
    """
    清理文件名，移除危险字符

    防御层级：
    1. 提取基本文件名（防止路径穿越）
    2. 替换危险字符
    3. 处理反斜杠（Windows 路径分隔符）
    4. 移除连续点号（防御纵深）
    5. 长度截断
    """
    # 提取文件名（Path.name 自动处理 / 和 \）
    name = Path(name).name.strip() or "未命名文件"

    # 清理危险字符（包括反斜杠）
    name = re.sub(r"[\x00-\x1f/:*?\"<>|\\]", "_", name)

    # 移除连续点号（防御纵深）
    name = re.sub(r"\.\.+", ".", name)

    # 移除开头的点和下划线
    name = re.sub(r"^[._]+", "", name) or "unnamed"

    return name[:max_len]


def contained_path(path: Path, root: Path) -> Path:
    resolved = path.resolve(strict=False)
    base = root.resolve(strict=True)
    if resolved != base and base not in resolved.parents:
        raise ValueError("文件路径超出案件目录")
    return resolved


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

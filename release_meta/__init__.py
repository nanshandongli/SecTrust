"""
发布元数据定位（release_meta/__init__.py）—— 找到随包分发的 mcpserver.json。

为什么需要这个模块：
`mcpserver.json` 是 MCP Registry 收录必需的文件（声明工具清单、协议版本、
能力注解）。但它有两个可能的落点：
  1. 仓库根目录（开发态：`pip install -e .` 或直接跑源码）
  2. 安装后的包内（发布态：`pip install sectrust`）

打包工具的 package-data **不能引用包外路径**，所以发布态的副本放在
`release_meta/mcpserver.json`，由本模块统一解析。找不到时返回 None——
调用方应明确报错，而不是静默用一份过期的清单。

⚠️ 包名**不能叫 `packaging`**：那是 PyPA 的核心库名（pip / build / setuptools
都依赖它），同名会导致装包后 `import packaging` 拿到本包，pip 全线崩溃。
"""

from __future__ import annotations

import json
import os
from typing import Optional

__all__ = ["mcpserver_path", "load_mcpserver", "repo_root"]

_PKG_DIR = os.path.dirname(os.path.abspath(__file__))


def repo_root() -> Optional[str]:
    """仓库根目录（开发态）。装包后没有源码树，返回 None。"""
    root = os.path.dirname(_PKG_DIR)
    if os.path.isfile(os.path.join(root, "pyproject.toml")):
        return root
    return None


def mcpserver_path() -> Optional[str]:
    """定位 mcpserver.json：先找包内副本，再找仓库根。找不到返回 None。"""
    candidates = [
        os.path.join(_PKG_DIR, "mcpserver.json"),          # 发布态（包内）
        os.path.join(repo_root() or "", "mcpserver.json"),  # 开发态（仓库根）
    ]
    for p in candidates:
        if p and os.path.isfile(p):
            return p
    return None


def load_mcpserver() -> Optional[dict]:
    """读取 mcpserver.json。找不到或解析失败返回 None。"""
    p = mcpserver_path()
    if not p:
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


if __name__ == "__main__":
    p = mcpserver_path()
    print("mcpserver.json:", p or "<<未找到>>")
    if p:
        data = load_mcpserver() or {}
        print("name    =", data.get("name"))
        print("version =", data.get("version"))
        print("tools   =", len(data.get("tools", [])))

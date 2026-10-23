"""
Mem0 / OpenMemory 适配器（mem0_adapter.py）

把"长期记忆"整体或可选地委托给 Mem0（开源记忆层，含语义检索/图谱）或 OpenMemory MCP。
对应路线 3 的"集成 Mem0 / OpenMemory MCP"。

默认不启用：本项目自带零依赖的记忆（SQLite + 轻量向量）已覆盖核心场景。若团队需要
更强的语义/时序/多用户合规，可在装好 mem0 后切换到本适配器——接口与 SemanticMemory 对齐，
runner / API 无感替换。
"""

from __future__ import annotations

from typing import List, Optional, Tuple


class Mem0Adapter:
    """Mem0 适配：若环境 pip install mem0ai 则委托，否则 available()=False。"""

    def __init__(self, user_id: str = "default", api_key: str = ""):
        self.user_id = user_id
        self.api_key = api_key or ""
        self._client = None

    def available(self) -> bool:
        try:
            from mem0 import Memory  # type: ignore
            self._client = Memory()
            return True
        except Exception:  # noqa: BLE001
            return False

    def remember(self, category: str, key: str, value, note: str = "") -> None:
        if not self.available():
            raise RuntimeError("Mem0 未安装（pip install mem0ai）")
        messages = [{"role": "user",
                     "content": f"[{category}] {key}: {value} {note}".strip()}]
        self._client.add(messages, user_id=self.user_id)

    def search_semantic(self, query: str, k: int = 5,
                        category: Optional[str] = None) -> List[Tuple[str, str, float, dict]]:
        if not self.available():
            raise RuntimeError("Mem0 未安装（pip install mem0ai）")
        res = self._client.search(query, user_id=self.user_id, limit=k)
        out = []
        for r in res.get("results", []):
            out.append(("mem0", r.get("id", ""), 1.0, r))
        return out


class OpenMemoryAdapter:
    """OpenMemory MCP 适配：通过 MCP 协议对接 OpenMemory（本地优先的记忆服务）。
    未配置 MCP 客户端时 available()=False。"""

    def __init__(self, mcp_client=None):
        self._mcp = mcp_client

    def available(self) -> bool:
        return self._mcp is not None

    def remember(self, category: str, key: str, value, note: str = "") -> None:
        if not self.available():
            raise RuntimeError("OpenMemory MCP 客户端未提供")
        self._mcp.call_tool("create_memory",
                            {"text": f"[{category}] {key}: {value} {note}".strip()})

    def search_semantic(self, query: str, k: int = 5, category=None) -> list:
        if not self.available():
            raise RuntimeError("OpenMemory MCP 客户端未提供")
        res = self._mcp.call_tool("search_memory", {"query": query, "limit": k})
        return [("openmemory", "", 1.0, r) for r in (res or [])]


if __name__ == "__main__":
    a = Mem0Adapter()
    print("Mem0 available:", a.available())
    b = OpenMemoryAdapter()
    print("OpenMemory available:", b.available())

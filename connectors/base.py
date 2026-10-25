"""
connectors/base.py —— 连接器统一抽象。

一个连接器 = 一组命名 action + 凭据可用性探测。上层（daemon/agent）通过
call(action, **params) 派发，不关心背后是 REST 还是 IMAP。

设计要点：
- available() 回答"此刻能不能用"（凭据是否配置、传输层是否注入）；
- call() 在不可用时**明确报错**，绝不返回假数据——这是全项目的诚实红线；
- actions() 返回机器可读的 action 清单（供 MCP 暴露/提案生成引用）。
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


class ConnectorError(RuntimeError):
    """连接器调用失败（不可用、网络错、上游拒绝等）。"""


@dataclass
class ConnectorResult:
    ok: bool
    action: str
    data: Any = None
    error: Optional[str] = None
    meta: Dict[str, Any] = field(default_factory=dict)


class Connector(abc.ABC):
    """连接器基类。子类实现 _actions() 与 _call()。"""

    name: str = "base"

    def __init__(self):
        self._last_error: Optional[str] = None

    # ---- 子类契约 ----
    @abc.abstractmethod
    def _actions(self) -> List[dict]:
        """返回 [{"name":..., "description":..., "params":{...}}, ...]。"""
        raise NotImplementedError

    @abc.abstractmethod
    def _configured(self) -> bool:
        """凭据/传输层是否就绪。"""
        raise NotImplementedError

    @abc.abstractmethod
    def _call(self, action: str, **params) -> Any:
        """真正执行。允许抛 ConnectorError。"""
        raise NotImplementedError

    # ---- 公开 API ----
    def actions(self) -> List[dict]:
        return self._actions()

    def available(self) -> bool:
        try:
            return self._configured()
        except Exception:
            return False

    def call(self, action: str, **params) -> ConnectorResult:
        known = {a["name"] for a in self._actions()}
        if action not in known:
            return ConnectorResult(ok=False, action=action,
                                   error="未知 action: %s（可用: %s）"
                                         % (action, sorted(known)))
        if not self.available():
            return ConnectorResult(
                ok=False, action=action,
                error="连接器 %s 未配置（缺少凭据或传输层）——不返回假数据" % self.name)
        try:
            data = self._call(action, **params)
            return ConnectorResult(ok=True, action=action, data=data,
                                   meta={"connector": self.name})
        except ConnectorError as e:
            self._last_error = str(e)
            return ConnectorResult(ok=False, action=action, error=str(e),
                                   meta={"connector": self.name})
        except Exception as e:               # 未预期异常也包装成结果，不裸抛
            self._last_error = "%s: %s" % (type(e).__name__, e)
            return ConnectorResult(ok=False, action=action, error=self._last_error,
                                   meta={"connector": self.name})

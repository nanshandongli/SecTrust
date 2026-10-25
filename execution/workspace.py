"""
execution/workspace.py —— 每个 Bot 的独立工作区（"自己的电脑"的本地最小版）。

Grok Bot 给每个 Bot 一台云端电脑（浏览器+终端+文件系统）。我们做本地版：
每个 bot_id 一个目录，内含：

    <root>/<bot_id>/
        work/        干活的地方：命令在此执行（沙箱 jail 根），产物落这里
        outbox/      待发送产物：经审批后才允许外发
        state.json   Bot 的持久状态（累计上下文，跨重启保留）

设计纪律：
- 命令执行一律委托 sandbox.SandboxManager.run()，本模块**绝不**直接起
  subprocess——越权预检/超时强杀/containment 标注必须走现成通道；
- state.json 读写带文件锁（fcntl，Windows 上退化为无锁但仍是原子替换），
  防止多线程/多进程同时写坏状态；
- 所有路径限制在 root 之内，bot_id 做白名单校验（防路径穿越）。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time

from sandbox import SandboxManager

# bot_id 白名单：字母数字点下划线短横，1~64 字符（防 "../" 路径穿越）
_BOT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

DEFAULT_ROOT = os.path.join(os.path.expanduser("~"), ".sectrust", "workspaces")


def default_root() -> str:
    """工作区根目录：默认 ~/.sectrust/workspaces，可用环境变量覆盖。"""
    return os.environ.get("SECTRUST_WORKSPACE_ROOT", DEFAULT_ROOT)


class WorkspaceError(ValueError):
    """工作区操作失败（非法 bot_id、越界路径等）。"""


def _validate_bot_id(bot_id: str) -> str:
    if not isinstance(bot_id, str) or not _BOT_ID_RE.match(bot_id):
        raise WorkspaceError(
            "非法 bot_id: %r（仅允许字母数字 . _ - ，首字符为字母数字，≤64 字符）"
            % (bot_id,)
        )
    return bot_id


class BotWorkspace:
    """一个 Bot 的独立工作区。

    用法：
        ws = BotWorkspace("sales-bot")
        ws.write_state({"voice": "简洁直接"})
        result = ws.run(["python", "-c", "print(1+1)"])   # 走沙箱
        ws.put_output("report.md", "...")                 # 落 outbox 待审批外发
    """

    def __init__(self, bot_id: str, root: str = None, sandbox: SandboxManager = None):
        _validate_bot_id(bot_id)
        self.bot_id = bot_id
        self.root = os.path.abspath(root or default_root())
        self.dir = os.path.join(self.root, bot_id)
        self.work_dir = os.path.join(self.dir, "work")
        self.outbox_dir = os.path.join(self.dir, "outbox")
        self.state_path = os.path.join(self.dir, "state.json")
        self._sandbox = sandbox or SandboxManager.auto()
        # RLock：write_state 持锁期间会调 read_state，普通 Lock 会自锁死锁
        self._lock = threading.RLock()   # 进程内串行化 state 读写
        os.makedirs(self.work_dir, exist_ok=True)
        os.makedirs(self.outbox_dir, exist_ok=True)

    # ---------- 路径安全：任何文件操作不得逃出 self.dir ----------
    def _resolve(self, *parts: str) -> str:
        for p in parts:
            if os.path.isabs(p) or ".." in p.replace("\\", "/").split("/"):
                raise WorkspaceError("拒绝越界路径: %r" % (p,))
        target = os.path.abspath(os.path.join(self.dir, *parts))
        if target != self.dir and not target.startswith(self.dir + os.sep):
            raise WorkspaceError("拒绝越界路径: %r" % (os.path.join(*parts),))
        return target

    # ---------- 持久状态：跨重启保留的 Bot 上下文 ----------
    def read_state(self) -> dict:
        with self._lock:
            try:
                with open(self.state_path, encoding="utf-8") as f:
                    data = json.load(f)
                return data if isinstance(data, dict) else {}
            except FileNotFoundError:
                return {}
            except (OSError, json.JSONDecodeError):
                # 状态损坏不静默重置：原样保留为 _corrupt，供人工/上层恢复
                return {"_corrupt": True}

    def write_state(self, updates: dict, merge: bool = True) -> dict:
        """写状态。merge=True 时与原状态合并；原子替换防写一半。"""
        if not isinstance(updates, dict):
            raise WorkspaceError("state 更新必须是 dict")
        with self._lock:
            current = self.read_state() if merge else {}
            current.update(updates)
            current["updated_at"] = time.time()
            fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".state-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(current, f, ensure_ascii=False, indent=1)
                os.replace(tmp, self.state_path)   # 同目录原子替换
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            return current

    # ---------- 命令执行：一律走沙箱，绝不直接 subprocess ----------
    def run(self, cmd, timeout: float = 30):
        """在 work/ 下执行命令。返回 sandbox.SandboxResult。

        诚实边界：本地后端 containment="advisory"（仅命令预检，不是 OS 级
        隔离）；装 Docker/E2B 的环境会自动升级到 containment="os"。
        """
        return self._sandbox.run(cmd, self.work_dir, timeout=timeout)

    # ---------- 产物管理 ----------
    def put_output(self, name: str, content: str) -> str:
        """写产物到 outbox/。外发前须经 ApprovalCenter 批准。"""
        path = self._resolve("outbox", name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path

    def list_outputs(self) -> list:
        try:
            return sorted(os.listdir(self.outbox_dir))
        except FileNotFoundError:
            return []

    def read_output(self, name: str) -> str:
        with open(self._resolve("outbox", name), encoding="utf-8") as f:
            return f.read()

    def __repr__(self):
        return "BotWorkspace(%r, root=%r)" % (self.bot_id, self.root)

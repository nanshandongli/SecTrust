"""
updates.py —— 「检查更新」：把本地 git 仓库与远端比对，回答"我是不是落后了"。

为什么用 git 而不是版本号/在线 API：
    本产品是本地跑的仓库，没有发布版本号概念；用 git 比对最直接，也不引入
    任何账号/密钥/第三方接口依赖。查的是远端引用（ls-remote），**只读**。

诚实边界（本模块最重要的部分）：
    - **查不到就说查不到**。不是 git 仓库、没配 remote、git 没装、网络不通、
      超时、远端没返回 HEAD —— 一律返回 state="unknown" 并给出具体原因。
      **绝不因为"没查到差异"就报"已是最新"**：那比不检查更危险，用户会以为
      自己在最新版，其实只是没连上。
    - 只读检查：只跑 `git ls-remote`（和本地 rev-parse/status），
      **不 fetch、不 pull、不改工作区、不动任何文件**。
    - 远端与本地不一致时如实说明两种可能：远端有更新，**或**本地有未推送的
      提交。判断"该 pull 还是该 push"是人的事，这里不替用户做。

设置：自动检查开关落在 data/update_settings.json（只存布尔开关与时间，
不存任何凭据）。
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time

# 单次 git 调用的超时：网络不通时不该把界面卡死
_GIT_TIMEOUT = float(os.environ.get("SECTRUST_UPDATE_TIMEOUT", "10"))

# state 取值说明
#   up_to_date   本地 HEAD 与远端 HEAD 一致
#   remote_newer 远端与本地不一致（远端有更新，或本地有未推送提交）
#   unknown      无法判定（原因见 reason）——**不是"已是最新"**
STATE_UP_TO_DATE = "up_to_date"
STATE_REMOTE_NEWER = "remote_newer"
STATE_UNKNOWN = "unknown"


def _run(args, cwd, timeout=_GIT_TIMEOUT):
    """跑一条 git 命令。返回 (stdout, error)；error 非 None 即失败。"""
    try:
        p = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                           timeout=timeout)
    except FileNotFoundError:
        return None, "git 不可用（未安装或不在 PATH 中）"
    except subprocess.TimeoutExpired:
        return None, "git 命令超时（%.0fs，网络慢或远端不可达）" % timeout
    except OSError as e:                      # noqa: BLE001
        return None, "git 调用失败：%s" % e
    if p.returncode != 0:
        detail = (p.stderr or p.stdout or "").strip()
        return None, (detail[:300] or "git 返回非零（%d）" % p.returncode)
    return (p.stdout or "").strip(), None


class UpdateChecker:
    """本地仓库 vs 远端：只读检查 + 开关设置。"""

    def __init__(self, repo_dir: str = None, data_dir: str = "data"):
        self.repo_dir = os.path.abspath(
            repo_dir or os.path.dirname(os.path.abspath(__file__)))
        self.data_dir = data_dir
        self.settings_path = os.path.join(data_dir, "update_settings.json")

    # ---------------------------------------------------------- 本地信息
    def _is_repo(self) -> bool:
        return os.path.isdir(os.path.join(self.repo_dir, ".git"))

    def local(self) -> dict:
        """本地版本信息：commit / 分支 / tag 描述 / 是否有未提交改动。"""
        if not self._is_repo():
            return {"ok": False, "reason": "不是 git 仓库：%s" % self.repo_dir}
        out, err = _run(["git", "rev-parse", "HEAD"], self.repo_dir)
        if err:
            return {"ok": False, "reason": err}
        head = (out.splitlines() or [""])[0].strip()
        branch, _ = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                         self.repo_dir)
        describe, _ = _run(["git", "describe", "--tags", "--always", "--dirty"],
                           self.repo_dir)
        status, _ = _run(["git", "status", "--porcelain"], self.repo_dir)
        changed = [ln for ln in (status or "").splitlines() if ln.strip()]
        return {
            "ok": True,
            "commit": head[:12],
            "commit_full": head,
            "branch": branch or "",
            "describe": describe or head[:12],
            "dirty": bool(changed),
            "dirty_files": len(changed),
        }

    # ---------------------------------------------------------- 远端比对
    def check(self, timeout: float = None) -> dict:
        """比对本地 HEAD 与远端 HEAD。只读，不 fetch / 不 pull。"""
        tmo = timeout if timeout is not None else _GIT_TIMEOUT
        loc = self.local()
        if not loc.get("ok"):
            return self._remember({"state": STATE_UNKNOWN,
                                   "reason": loc.get("reason"), "local": loc})

        remotes, err = _run(["git", "remote"], self.repo_dir, tmo)
        if err:
            return self._remember({"state": STATE_UNKNOWN, "reason": err,
                                   "local": loc})
        names = [r.strip() for r in (remotes or "").splitlines() if r.strip()]
        if not names:
            return self._remember({
                "state": STATE_UNKNOWN, "local": loc,
                "reason": "仓库没有配置 git remote，无法与远端比对"})

        remote_name = "origin" if "origin" in names else names[0]
        out, err = _run(["git", "ls-remote", remote_name, "HEAD"],
                        self.repo_dir, tmo)
        if err:
            return self._remember({
                "state": STATE_UNKNOWN, "local": loc,
                "remote_name": remote_name,
                "reason": "无法访问远端 %s：%s" % (remote_name, err)})

        remote_head = ""
        for line in (out or "").splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1].upper() == "HEAD":
                remote_head = parts[0].strip()
                break
        if not remote_head:
            return self._remember({
                "state": STATE_UNKNOWN, "local": loc,
                "remote_name": remote_name,
                "reason": "远端 %s 没有返回 HEAD 引用" % remote_name})

        local_head = loc.get("commit_full") or ""
        if remote_head == local_head:
            res = {
                "state": STATE_UP_TO_DATE, "local": loc,
                "remote_name": remote_name,
                "remote_commit": remote_head[:12],
                "remote_commit_full": remote_head,
                "message": "已是最新：本地 HEAD 与远端 %s 一致" % remote_name,
            }
        else:
            res = {
                "state": STATE_REMOTE_NEWER, "local": loc,
                "remote_name": remote_name,
                "remote_commit": remote_head[:12],
                "remote_commit_full": remote_head,
                "message": ("远端 %s 与本地不一致（远端 %s / 本地 %s）。"
                            "这可能是远端有新提交，也可能是本地有未推送的提交——"
                            "用 git log / git status 确认后再决定 pull 还是 push。"
                            % (remote_name, remote_head[:12], local_head[:12])),
            }
        if loc.get("dirty"):
            res["warning"] = ("本地有 %d 个未提交改动，更新前请先处理"
                              % loc["dirty_files"])
        return self._remember(res)

    # ---------------------------------------------------------- 设置
    def _defaults(self) -> dict:
        return {"auto_check": False, "last_checked": None, "last_state": None}

    def settings(self) -> dict:
        d = self._defaults()
        try:
            with open(self.settings_path, encoding="utf-8") as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                for k in d:
                    if k in saved:
                        d[k] = saved[k]
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            pass        # 没存过 / 损坏 → 用默认值，不静默新建文件
        return d

    def _write(self, data: dict) -> dict:
        """原子写设置文件（同目录临时文件 + os.replace）。"""
        os.makedirs(self.data_dir, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.data_dir, prefix=".upd-",
                                   suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.settings_path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return data

    def save_settings(self, updates: dict) -> dict:
        """只接受 auto_check（布尔）与 last_* 记账字段；其余键一律忽略。"""
        cur = self.settings()
        if isinstance(updates, dict):
            if "auto_check" in updates:
                cur["auto_check"] = bool(updates["auto_check"])
            for k in ("last_checked", "last_state"):
                if k in updates:
                    cur[k] = updates[k]
        return self._write(cur)

    def _remember(self, res: dict) -> dict:
        """把本次结论记进设置（供下次打开界面时显示"上次检查"）。"""
        if not isinstance(res, dict):
            return res
        res["checked_at"] = time.time()
        try:
            self.save_settings({"last_checked": res["checked_at"],
                                "last_state": res.get("state")})
        except OSError:
            pass        # 记不住也不影响本次检查结论
        return res

"""
最小可复现 Demo —— /scan repo 全流程（Human-in-the-loop + 沙箱隔离 + live_view + 回推 IM）

链路：
  飞书/Discord(此处用 MockChannel 模拟) 发 "/scan repo <url>"
    -> 进入审批中心(pending)                   [Human-in-the-loop]
    -> 真人批准 / 自动批准(--auto-approve)
    -> 沙箱隔离执行「低强度扫描」             [沙箱隔离 + 文件越权拦截]
    -> 进度实时写入登记处                       [跨进程任务审计 / live_view]
    -> 结果经 channel 回推 IM                   [结果回推]

3 分钟可复现（离线、无需任何密钥）：
  # 终端 A：启动 live_view 查看端
  python task_center_web.py --db-path data/task_registry.db --port 8090
  # 终端 B：跑 demo（默认 2 秒后自动批准，方便一键看完整流程）
  python examples/scan_repo_demo.py --auto-approve

真人审批版（不开自动批准，演示真正的 Human-in-the-loop）：
  python examples/scan_repo_demo.py
  # 另开终端批准：
  python approval.py --list
  python approval.py --approve <approval_id>

说明：
- 为离线可复现，默认扫描本地 examples/sample_repo（已复制进沙箱 jail 目录）。
  生产环境可替换为 `git clone <repo_url>` 到沙箱工作目录后再扫描。
- 沙箱后端默认 auto（docker 可用则强隔离，否则本地受限降级）。可用 --backend 指定。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import threading
import time
import uuid

# 允许以 `python examples/scan_repo_demo.py` 直接运行：把仓库根加入导入路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import load_config
from task_registry import TaskRegistry
from approval import ApprovalCenter
from channels.mock import MockChannel
from channels import InboundMessage, OutboundMessage, _new_id
from sandbox import SandboxManager, safe_scan_command, DockerSandbox, LocalSandbox


HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLE_REPO = os.path.join(HERE, "sample_repo")


class ScanRepoDemo:
    def __init__(self, cfg, backend: str, auto_approve: bool, repo_dir: str):
        self.cfg = cfg
        self.reg = TaskRegistry(db_path=cfg.db_path)
        self.ac = ApprovalCenter(db_path=cfg.db_path)
        # SandboxManager() 默认调用 auto() 探测后端；显式指定时直接包装
        self.sb = SandboxManager() if backend == "auto" else \
            SandboxManager(DockerSandbox() if backend == "docker" else LocalSandbox())
        self.channel = MockChannel(cfg.channel)  # 仅用于 send 回推
        self.auto_approve = auto_approve
        self.repo_dir = repo_dir

    # 模拟"飞书/Discord 收到 /scan repo"
    def simulate_inbound(self, text: str, user_id: str = "feishu-user-1"):
        msg = InboundMessage(
            channel="feishu", message_id=_new_id("feishu"), user_id=user_id, text=text
        )
        self.handle(msg)

    def handle(self, inbound: InboundMessage):
        text = inbound.text.strip()
        if not text.startswith("/scan"):
            print(f"[demo] 非扫描命令，忽略: {text}")
            return

        repo_url = text[len("/scan"):].strip() or "(本地样例仓库)"
        tid = f"scan-{int(time.time())}-{uuid.uuid4().hex[:6]}"
        self.reg.register_task(
            tid,
            payload={"channel": inbound.channel, "user_id": inbound.user_id,
                     "action": "scan_repo", "repo": repo_url},
        )
        self.reg.update_task_status(tid, "running")
        print(f"[demo] 收到 /scan repo: {repo_url}  -> 任务 {tid}")

        # 1) 进入审批中心（Human-in-the-loop，默认拒绝）
        aid = self.ac.request(
            "scan_repo", repo_url, task_id=tid,
            channel=inbound.channel, user_id=inbound.user_id,
        )
        self.reg.set_progress(tid, "pending_approval", f"进入审批中心 {aid}，等待批准…")
        print(f"[demo] 已挂起至审批中心: {aid}（状态=pending，默认拒绝）")

        if self.auto_approve:
            # 演示用：模拟审批人在 2 秒后点击「批准」
            threading.Timer(2.0, lambda: self.ac.decide(aid, "approve", by="auto-demo")).start()
        else:
            print(f"[demo] 请在另一终端批准： python approval.py --approve {aid}")

        decision = self.ac.wait_for_decision(aid, timeout=120)
        if decision != "approved":
            self.reg.set_progress(tid, "rejected", f"审批结果: {decision}（已拦截，未执行）")
            self.reg.update_task_status(tid, "failed")
            self.channel.send(OutboundMessage(
                channel=inbound.channel, user_id=inbound.user_id,
                text=f"⛔ 扫描被拦截（审批={decision}），未执行任何操作。"))
            print(f"[demo] 审批={decision}，已拦截，不执行沙箱。")
            return

        # 2) 沙箱隔离执行低强度扫描
        self.reg.set_progress(tid, "approved", "已批准，准备进入沙箱…")
        workdir = os.path.abspath(os.path.join(".sandbox", tid))
        shutil.rmtree(workdir, ignore_errors=True)
        # 把目标仓库复制进 jail（生产替换为 git clone）
        shutil.copytree(self.repo_dir, workdir)
        self.reg.set_progress(tid, "sandboxing", f"沙箱后端={self.sb.backend.name}，工作目录已 jail")
        print(f"[demo] 进入沙箱[{self.sb.backend.name}]，扫描 {workdir}")

        res = self.sb.run(safe_scan_command(workdir), workdir, timeout=30)
        self.reg.set_progress(tid, "scanning", "沙箱内扫描执行中…")

        if not res.ok:
            self.reg.set_progress(tid, "sandbox_error", f"沙箱执行失败: {res.error}")
            self.reg.update_task_status(tid, "failed")
            self.channel.send(OutboundMessage(
                channel=inbound.channel, user_id=inbound.user_id,
                text=f"⚠️ 沙箱扫描失败（后端={res.backend}）：{res.error}"))
            print(f"[demo] 沙箱执行失败: {res.error}")
            return

        # 3) 解析扫描结果
        try:
            report = json.loads(res.content.strip().splitlines()[-1]) if res.content.strip() else {}
        except Exception:
            report = {"raw": res.content[:500]}
        self.reg.set_progress(tid, 100, "扫描完成，结果已生成")
        self.reg.update_task_status(tid, "done")

        summary = (
            f"✅ 扫描完成（沙箱后端={res.backend}，耗时 {res.duration:.1f}s）\n"
            f"扫描文件: {report.get('scanned_files','?')} 个\n"
            f"代码行数: {report.get('total_lines','?')}\n"
            f"TODO/FIXME 标记: {report.get('todo_markers','?')} 处\n"
            f"疑似硬编码密钥: {report.get('possible_secrets','?')} 处"
        )
        self.channel.send(OutboundMessage(
            channel=inbound.channel, user_id=inbound.user_id, text=summary))
        print(f"[demo] 已回推结果到 {inbound.channel}/{inbound.user_id}:")
        print(summary)

    def close(self):
        self.reg.close()
        self.ac.close()
        # 清理本次 Demo 产生的沙箱工作目录
        shutil.rmtree(".sandbox", ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description="/scan repo 最小可复现 Demo")
    ap.add_argument("--db-path", default=None)
    ap.add_argument("--backend", default="auto", choices=["auto", "docker", "local"])
    ap.add_argument("--auto-approve", action="store_true",
                    help="2 秒后自动批准（默认开启，便于一键看完整流程）")
    ap.add_argument("--no-auto-approve", dest="auto_approve", action="store_false",
                    help="关闭自动批准，演示真正的 Human-in-the-loop（需手动 CLI 批准）")
    ap.add_argument("--repo-dir", default=SAMPLE_REPO, help="被扫描仓库目录")
    ap.set_defaults(auto_approve=True)
    args = ap.parse_args()

    cfg = load_config()
    if args.db_path:
        cfg.db_path = args.db_path

    demo = ScanRepoDemo(cfg, args.backend, args.auto_approve, args.repo_dir)
    try:
        print("=" * 60)
        print(" /scan repo Demo —— Human-in-the-loop + 沙箱隔离 + live_view")
        print(" 查看实时进度：另开终端运行")
        print(f"   python task_center_web.py --db-path {cfg.db_path} --port 8090")
        print(" 然后浏览器打开 http://127.0.0.1:8090/")
        print("=" * 60)
        # 模拟飞书/Discord 发来一条 /scan repo
        demo.simulate_inbound("/scan repo https://github.com/example/sample-repo")
        # 等流程跑完（审批等待 + 沙箱执行）
        time.sleep(8)
    finally:
        demo.close()
        print("[demo] 结束")


if __name__ == "__main__":
    main()

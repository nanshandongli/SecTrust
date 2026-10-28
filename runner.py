"""
AgentRunner —— 多层运行时的组合根。

把三层扩展点（渠道 / 模型 / 技能）与现有「跨进程任务登记处」接起来：
  收消息 -> 登记 task_registry -> 调模型 -> (可选)执行技能 -> 回消息 -> 更新状态 + 心跳。

零配置即可跑：
  python runner.py --demo          # MockChannel + MockModel，离线端到端
接入真实渠道/模型（凭据全走环境变量，见 config.py / .env.example）：
  python runner.py --channel wecom --model gpt-4o-mini
  python runner.py --channel feishu --model claude-3-5-sonnet-latest
"""

from __future__ import annotations

import argparse
import signal
import threading
import time

from config import load_config, RunnerConfig
from task_registry import TaskRegistry
from channels import ChannelAdapter, InboundMessage, OutboundMessage
from channels.mock import MockChannel
from channels.wecom import WeComChannel
from channels.feishu import FeishuChannel
from models import ModelRouter, ChatMessage
from models.mock import MockModelProvider
from models.openai_like import OpenAIModelProvider
from models.anthropic import AnthropicProvider
from models.ollama import OllamaProvider
from skills import SkillRegistry, RunContext
from skills.local import LocalSkillProvider
from skills.github_source import GitHubSkillProvider
from api.client import ApiClient, RemoteRegistry


# ---------------- 工厂 ----------------
def build_channel(config: RunnerConfig) -> ChannelAdapter:
    name = config.channel.name
    if name == "mock":
        return MockChannel(config.channel)
    if name == "wecom":
        return WeComChannel(config.channel)
    if name == "feishu":
        return FeishuChannel(config.channel)
    print(f"[runner] 未知渠道 '{name}'，回退到 mock")
    return MockChannel(config.channel)


def build_model_router(config: RunnerConfig) -> ModelRouter:
    md = config.model
    providers = [
        OpenAIModelProvider(md.openai_api_key, md.openai_base_url),
        AnthropicProvider(md.anthropic_api_key),
        OllamaProvider(md.ollama_base_url),
    ]
    return ModelRouter(providers, default_model=md.default_model)


def build_skill_registry(config: RunnerConfig) -> SkillRegistry:
    sk = config.skill
    registry = SkillRegistry()
    local = LocalSkillProvider(sk.local_dir)
    local.sync()
    registry.add_provider(local)
    if sk.github_repo:
        gh = GitHubSkillProvider(sk.github_repo, sk.github_ref, sk.github_cache_dir, sk.allowlist)
        try:
            gh.sync()
            registry.add_provider(gh)
        except Exception as e:  # 拉取失败不应让运行时崩，记日志即可
            print(f"[runner] GitHub 技能源同步失败（已跳过）: {e}")
    return registry


# ---------------- 运行时 ----------------
class AgentRunner:
    def __init__(self, config: RunnerConfig, registry=None):
        self.config = config
        # 分布式：若传入 registry（如 RemoteRegistry 走 HTTP API）则用之；否则本地 SQLite
        self.reg = registry or TaskRegistry(db_path=config.db_path, runner_id=config.runner_id)
        self.channel = build_channel(config)
        self.model = build_model_router(config)
        self.skills = build_skill_registry(config)
        self.runner_id = self.reg.runner_id
        self._stop = threading.Event()
        self._counter = 0
        self._lock = threading.Lock()
        print(
            f"[runner:{self.runner_id}] 启动 | 渠道={self.channel.name} | "
            f"模型={self.config.model.default_model} | "
            f"技能={[s.name for s in self.skills.list_skills()]}"
        )

    def _next_task_id(self):
        with self._lock:
            self._counter += 1
            return f"task-{int(time.time())}-{self._counter:04d}"

    # 心跳线程
    def _heartbeat_loop(self):
        self.reg.beat()
        while not self._stop.wait(self.config.heartbeat_interval):
            self.reg.beat()

    # 消息处理主流程
    def _on_message(self, inbound: InboundMessage):
        # 先解析技能名（用于登记 payload）
        skill_used = None
        if inbound.text.startswith("/"):
            name = inbound.text[1:].partition(" ")[0]
            if self.skills.get(name):
                skill_used = name

        tid = self._next_task_id()
        self.reg.register_task(
            tid,
            payload={
                "channel": inbound.channel,
                "user_id": inbound.user_id,
                "model": self.config.model.default_model,
                "skill": skill_used,
                "text": inbound.text,
            },
        )
        self.reg.update_task_status(tid, "running")
        try:
            # 1) 模型推理
            messages = [
                ChatMessage(role="system", content="你是接入多渠道的 AI 助手。"),
                ChatMessage(role="user", content=inbound.text),
            ]
            result = self.model.complete(messages, model=self.config.model.default_model)
            parts = [result.content]

            # 2) 技能：/技能名 参数
            if skill_used:
                arg = inbound.text[1 + len(skill_used):].strip()
                skill = self.skills.get(skill_used)
                ctx = RunContext(model=self.model, config=self.config)
                sres = skill.entry({"text": arg}, ctx)
                parts.append(sres.content if sres.ok else f"[技能错误] {sres.error}")
                parts.extend(ctx.replies)

            reply_text = "\n".join(p for p in parts if p)
            # 3) 回消息
            self.channel.send(
                OutboundMessage(channel=inbound.channel, user_id=inbound.user_id, text=reply_text)
            )
            self.reg.update_task_status(tid, "done")
            self._notify_terminal(tid, "done")
        except Exception as e:  # noqa: BLE001
            self.reg.update_task_status(tid, "failed")
            self._notify_terminal(tid, "failed", str(e))
            print(f"[runner] 处理 {tid} 失败: {e}")

    def _notify_terminal(self, tid, status, detail=""):
        """任务终态 → 远程通道推送（飞书/企微）。**异常一律吞掉**（旁路纪律）。"""
        try:
            import notify as _notify
            _notify.push_task_event(status, tid, detail=detail)
        except Exception:  # noqa: BLE001 —— 推送绝不挡执行
            pass

    def run(self, duration: float = None):
        hb = threading.Thread(target=self._heartbeat_loop, daemon=True)
        hb.start()
        if duration:
            threading.Timer(duration, self.stop).start()
        try:
            self.channel.start(self._on_message)  # 阻塞（Mock 用线程 join；HTTP 渠道用 serve_forever）
        finally:
            self._stop.set()
            hb.join(timeout=1)
            self.channel.stop()
            self.reg.close()
            print(f"[runner:{self.runner_id}] 已停止")

    def stop(self):
        self._stop.set()
        self.channel.stop()


def _install_signal_handlers(runner: AgentRunner):
    def _handler(signum, _frame):
        print(f"收到信号 {signum}，准备退出…")
        runner.stop()
    signal.signal(signal.SIGINT, _handler)
    try:
        signal.signal(signal.SIGTERM, _handler)
    except (ValueError, AttributeError):
        pass


def main():
    ap = argparse.ArgumentParser(description="多渠道/多技能/多模型 agent 运行时")
    ap.add_argument("--demo", action="store_true", help="强制 mock 渠道 + mock 模型，离线端到端")
    ap.add_argument("--db-path", default=None)
    ap.add_argument("--channel", default=None, help="mock / wecom / feishu")
    ap.add_argument("--model", default=None, help="默认模型名，如 gpt-4o-mini / claude-... / ollama/llama3 / mock")
    ap.add_argument("--duration", type=float, default=None, help="运行 N 秒后自动退出（测试用）")
    ap.add_argument("--config", default=None, help="可选 JSON 配置文件路径")
    ap.add_argument("--api-base", default=None,
                    help="远程注册中心 URL（如 http://host:8080）；指定后 runner 跨机写登记处")
    ap.add_argument("--api-token", default=None, help="远程 API 的 Bearer 令牌")
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.db_path:
        cfg.db_path = args.db_path
    if args.channel:
        cfg.channel.name = args.channel
    if args.model:
        cfg.model.default_model = args.model
    if args.demo:
        cfg.channel.name = "mock"
        cfg.model.default_model = "mock"

    # 分布式：指定 --api-base 时，runner 通过 HTTP 远程写登记处/心跳
    remote = None
    if args.api_base:
        client = ApiClient(args.api_base, token=args.api_token)
        remote = RemoteRegistry(client, runner_id=cfg.runner_id or "remote-runner")

    runner = AgentRunner(cfg, registry=remote)
    _install_signal_handlers(runner)
    runner.run(duration=args.duration)


if __name__ == "__main__":
    main()

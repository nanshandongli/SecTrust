"""
分布式 API 端到端演示（examples/api_demo.py）

展示"路线 1：放弃同机 SQLite 前提"——把内核暴露为 HTTP，runner 通过远程客户端写登记处/记忆。

本脚本在同一进程内起一个 REST 中心（线程），然后用 ApiClient 模拟"远程 runner"：
  - 注册任务、更新状态、心跳（这些写操作全部打到 HTTP，而非本地 SQLite）；
  - 写入长期记忆（跨会话偏好：代码审查用 Qwen-Max）；
  - 列模板、查健康。

真实部署时：中心跑在 A 机（python -m api.app --auth --api-tokens xxx），
runner 跑在 B 机（python runner.py --api-base http://A:8080 --api-token xxx）。
"""

import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.app import RegistryAPI
from api.client import ApiClient


def main():
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "reg.db")
    mb = os.path.join(tmp, "mem.db")

    # 1) 起中心（若设了 API_AUTH_ENABLED / API_TOKENS 即开启鉴权）
    api = RegistryAPI(db_path=db, memory_db_path=mb, host="127.0.0.1", port=8231,
                      require_auth=False)
    srv = threading.Thread(target=api.serve, daemon=True)
    srv.start()
    time.sleep(0.5)

    # 2) 远程 runner 通过 HTTP 客户端写登记处/记忆
    client = ApiClient("http://127.0.0.1:8231")

    print("== 健康检查 ==")
    print("health:", client.health())

    print("\n== 远程写入任务登记处 ==")
    tid = client.register_task(None, {"source": "remote-runner", "text": "帮我审查这段 PR"})
    client.update_task_status(tid, "running")
    client.beat("remote-runner-01")
    print("task_id:", tid, "| runners:", [r["runner_id"] for r in client.list_runners()])

    print("\n== 跨会话长期记忆（远程写入偏好）==")
    client.memory_set("preference", "code_review_model", "Qwen-Max",
                     note="用户偏好用 Qwen-Max 做代码审查")
    print("code_review_model =>",
          client.memory_get("preference", "code_review_model")["value"])

    print("\n== 合规扫描模板（远程可列）==")
    print("templates:", [t["id"] for t in client.list_templates()])

    # 3) 收尾
    api.shutdown()
    time.sleep(0.3)
    print("\n演示结束：所有写操作均经 HTTP 完成，runner 与中心可跨机部署。")


if __name__ == "__main__":
    main()

"""
HTTP 远程客户端（api/client.py）—— 让 runner / 外部 agent 像调本地 store 一样调远程 API。

设计目标（路线 1：分布式）：
- 与本地 TaskRegistry / ApprovalCenter / MemoryStore 暴露"同类能力"，runner 可无感切换
  本地 SQLite 或远程 HTTP（见 runner 的 --api-base 选项）；
- 鉴权：Bearer 令牌（OAuth 2.1）；缺省无；
- 零依赖：标准库 urllib。

典型用法：
  client = ApiClient("http://localhost:8080", token=os.getenv("API_TOKEN"))
  client.register_task("t1", {"x": 1})
  client.memory_set("preference", "code_review_model", "Qwen-Max")
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional


class ApiClient:
    def __init__(self, base_url: str, token: Optional[str] = None, timeout: float = 30):
        self.base = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    # ---- 底层 ----
    def _req(self, method: str, path: str, body: Optional[Dict] = None,
             params: Optional[Dict] = None) -> Any:
        url = self.base + path
        if params:
            from urllib.parse import urlencode
            url += "?" + urlencode(params)
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:  # noqa: S310
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            raise RuntimeError(f"API {method} {path} -> {e.code}: {detail}") from e

    # ---- 任务登记处 ----
    def register_task(self, task_id: Optional[str], payload: Dict) -> str:
        return self._req("POST", "/tasks", {"task_id": task_id, "payload": payload})["task_id"]

    def list_tasks(self, status: Optional[str] = None, runner_id: Optional[str] = None,
                   limit: int = 100) -> List[Dict]:
        return self._req("GET", "/tasks", params={
            "status": status, "runner_id": runner_id, "limit": limit})

    def get_task(self, task_id: str) -> Optional[Dict]:
        return self._req("GET", f"/tasks/{task_id}")

    def update_task_status(self, task_id: str, status: str) -> bool:
        return self._req("POST", f"/tasks/{task_id}/status", {"status": status})["updated"]

    def set_progress(self, task_id: str, progress: Any, note: str = "") -> bool:
        return self._req("POST", f"/tasks/{task_id}/progress",
                         {"progress": progress, "note": note})["updated"]

    def beat(self, runner_id: str = "api") -> bool:
        return self._req("POST", "/runners/beat", {"runner_id": runner_id})["beat"]

    def list_runners(self) -> List[Dict]:
        return self._req("GET", "/runners")

    # ---- 审批中心 ----
    def request_approval(self, action: str, target: str, task_id: Optional[str] = None,
                         channel: str = "api", user_id: str = "api") -> str:
        return self._req("POST", "/approvals/request",
                         {"action": action, "target": target, "task_id": task_id,
                          "channel": channel, "user_id": user_id})["approval_id"]

    def decide_approval(self, approval_id: str, decision: str, by: str = "api",
                        note: str = "") -> Dict:
        return self._req("POST", f"/approvals/{approval_id}/decide",
                         {"decision": decision, "by": by, "note": note})

    def list_approvals(self, status: Optional[str] = None, limit: int = 100) -> List[Dict]:
        return self._req("GET", "/approvals", params={"status": status, "limit": limit})

    # ---- 内存/记忆 ----
    def memory_set(self, category: str, key: str, value: Any, note: str = "") -> Dict:
        return self._req("POST", "/memory",
                         {"category": category, "key": key, "value": value, "note": note})

    def memory_get(self, category: str, key: str) -> Optional[Dict]:
        return self._req("GET", f"/memory/{category}/{key}")

    def memory_list(self, category: Optional[str] = None, query: Optional[str] = None,
                   top: Optional[int] = None, limit: int = 100) -> List[Dict]:
        params: Dict[str, Any] = {"limit": limit}
        if category:
            params["category"] = category
        if query:
            params["query"] = query
        if top:
            params["top"] = top
        return self._req("GET", "/memory", params=params)

    def memory_forget(self, category: str, key: str) -> Dict:
        return self._req("DELETE", f"/memory/{category}/{key}")

    # ---- 扫描 ----
    def list_templates(self) -> List[Dict]:
        return self._req("GET", "/templates")

    def request_scan(self, template_id: str, repo_dir: str, **kw) -> Dict:
        return self._req("POST", "/scan/request",
                         {"template_id": template_id, "repo_dir": repo_dir, **kw})

    def execute_scan(self, task_id: str, approval_id: str, repo_dir: str, **kw) -> Dict:
        return self._req("POST", "/scan/execute",
                         {"task_id": task_id, "approval_id": approval_id,
                          "repo_dir": repo_dir, **kw})

    def health(self) -> Dict:
        return self._req("GET", "/health")


class RemoteRegistry:
    """适配 AgentRunner 的「任务登记处」接口，使其可通过 HTTP 远程写登记处/心跳。

    让 runner 与中心 API 解耦到不同机器（路线 1：放弃同机 SQLite 前提）。
    仅实现 AgentRunner 实际调用的子集：register_task / update_task_status /
    beat / close / runner_id。"""

    def __init__(self, client: ApiClient, runner_id: str = "remote-runner"):
        self.client = client
        self.runner_id = runner_id
        self._closed = False

    def register_task(self, task_id: str, runner_id: str = None, payload: Dict = None) -> str:
        return self.client.register_task(task_id, payload or {})

    def update_task_status(self, task_id: str, status: str) -> bool:
        return self.client.update_task_status(task_id, status)

    def beat(self) -> bool:
        return self.client.beat(self.runner_id)

    def close(self) -> None:
        self._closed = True


if __name__ == "__main__":
    import os
    c = ApiClient(os.getenv("API_BASE", "http://localhost:8080"),
                  token=os.getenv("API_TOKEN"))
    print("health:", c.health())
    print("templates:", [t["id"] for t in c.list_templates()])

"""
asset_registry.py —— 资产盘点与管理（国标六项能力之"资产盘点与管理"）

清点平台上的"智能体资产"：AI 同事（agents）/ 工具（tools）/ 模型（models）/
数据源（data_sources）。这是合规盘点的底座——回答"这个环境里到底有哪些
能自主行动/处理数据的实体"，也是应急处置时"该拦哪些"的依据。

设计：
  · 落盘 data/asset_registry.json（进程无关，控制台与 daemon 共享）；
  · 既支持人工登记（register），也支持从既有平台结构自动发现（discover）：
    agents ← console_api.ROLES；models ← model_providers.catalog()；
    tools ← skills 目录；data_sources ← assets 素材库。
  · 每条资产带分类分级（classify：公开/内部/受限/涉密）与负责人（owner），
    供"知识库与数据管控"与应急处置联动。
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Dict, List, Optional

DEFAULT_PATH = os.path.join("data", "asset_registry.json")

# 资产分类分级（数据管控维度）
CLASSIFICATIONS = ("public", "internal", "restricted", "confidential")
CLASSIFICATION_CN = {
    "public": "公开", "internal": "内部", "restricted": "受限", "confidential": "涉密",
}
KINDS = ("agent", "tool", "model", "data_source")


class AssetRegistry:
    """资产清册（JSON 落盘，线程安全）。"""

    def __init__(self, path: str = DEFAULT_PATH):
        self.path = path
        self._lock = threading.RLock()
        self._cache: Optional[Dict] = None

    def _read(self) -> Dict:
        with self._lock:
            if self._cache is not None:
                return self._cache
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, ValueError):
                data = {"assets": {}, "discovered_at": None}
            if not isinstance(data, dict) or "assets" not in data:
                data = {"assets": {}, "discovered_at": None}
            self._cache = data
            return data

    def _write(self, data: Dict) -> None:
        with self._lock:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
            self._cache = data

    def register(self, kind: str, asset_id: str, name: str,
                 classification: str = "internal", owner: str = "unknown",
                 meta: Optional[dict] = None) -> Dict:
        """登记一项资产（人工或自动发现都走这里）。"""
        if kind not in KINDS:
            raise ValueError("未知资产类型：%s（应为 %s）" % (kind, "/".join(KINDS)))
        if classification not in CLASSIFICATIONS:
            raise ValueError("未知分级：%s" % classification)
        data = self._read()
        data["assets"][asset_id] = {
            "id": asset_id, "kind": kind, "name": name,
            "classification": classification, "owner": owner,
            "meta": meta or {}, "registered_at": time.time(),
            "source": "manual",
        }
        self._write(data)
        return data["assets"][asset_id]

    def remove(self, asset_id: str) -> bool:
        data = self._read()
        if asset_id in data["assets"]:
            del data["assets"][asset_id]
            self._write(data)
            return True
        return False

    def list(self, kind: Optional[str] = None) -> List[Dict]:
        data = self._read()
        out = [v for v in data["assets"].values()
               if kind is None or v["kind"] == kind]
        return sorted(out, key=lambda x: (x["kind"], x["id"]))

    def summary(self) -> Dict:
        data = self._read()
        by_kind: Dict[str, int] = {}
        by_class: Dict[str, int] = {}
        for v in data["assets"].values():
            by_kind[v["kind"]] = by_kind.get(v["kind"], 0) + 1
            by_class[v["classification"]] = by_class.get(v["classification"], 0) + 1
        return {
            "total": len(data["assets"]),
            "by_kind": by_kind,
            "by_classification": by_class,
            "discovered_at": data.get("discovered_at"),
        }

    def discover(self, project_root: str = ".") -> Dict:
        """从既有平台结构自动发现并登记资产（幂等：已存在则跳过）。"""
        data = self._read()
        added = 0

        # 1) AI 同事 ← console_api.ROLES（岗位即"可调度智能体"）
        try:
            import console_api  # 延迟导入，避免循环
            for r in console_api.ROLES:
                aid = "agent:" + r["id"]
                if aid not in data["assets"]:
                    data["assets"][aid] = {
                        "id": aid, "kind": "agent", "name": r["name"],
                        "classification": "internal", "owner": "platform",
                        "meta": {"desc": r["desc"], "skills": r.get("skills", []),
                                 "model": r.get("model")},
                        "registered_at": time.time(), "source": "discovered",
                    }
                    added += 1
        except Exception:  # noqa: BLE001 —— 发现失败不影响主流程
            pass

        # 2) 模型 ← model_providers.catalog()
        try:
            from model_providers import catalog as _mc
            cat = _mc() if callable(_mc) else _mc
            models = cat.get("models", []) if isinstance(cat, dict) else []
            for m in models:
                mid = "model:" + str(m.get("id") or m.get("name") or m)
                if mid not in data["assets"]:
                    data["assets"][mid] = {
                        "id": mid, "kind": "model",
                        "name": str(m.get("name") or m.get("id") or m),
                        "classification": "restricted",
                        "owner": str(m.get("provider") or "platform"),
                        "meta": {k: m[k] for k in ("provider", "family")
                                 if k in m},
                        "registered_at": time.time(), "source": "discovered",
                    }
                    added += 1
        except Exception:  # noqa: BLE001
            pass

        # 3) 工具 ← skills 目录（每个技能即一项工具能力）
        skills_dir = os.path.join(project_root, "skills")
        if os.path.isdir(skills_dir):
            for name in os.listdir(skills_dir):
                if name.startswith("."):
                    continue
                tid = "tool:" + name
                if tid not in data["assets"]:
                    data["assets"][tid] = {
                        "id": tid, "kind": "tool", "name": name,
                        "classification": "internal", "owner": "platform",
                        "meta": {"location": "skills/" + name},
                        "registered_at": time.time(), "source": "discovered",
                    }
                    added += 1

        # 4) 数据源 ← assets 素材库（内容寻址素材库即一类受控数据源）
        data["assets"].setdefault("datasource:asset_store", {
            "id": "datasource:asset_store", "kind": "data_source",
            "name": "共享素材库（内容寻址）", "classification": "internal",
            "owner": "platform",
            "meta": {"location": "data/assets.db", "content_addressed": True},
            "registered_at": time.time(), "source": "discovered",
        })
        if "datasource:asset_store" not in data["assets"] or \
                data["assets"]["datasource:asset_store"]["source"] != "discovered":
            added += 1

        data["discovered_at"] = time.time()
        self._write(data)
        return {"added": added, "total": len(data["assets"])}


# 模块级便捷实例
_default = AssetRegistry()


def discover(project_root: str = ".") -> Dict:
    return AssetRegistry().discover(project_root=project_root)


def summary(path: str = DEFAULT_PATH) -> Dict:
    return AssetRegistry(path).summary()


def list_assets(kind: Optional[str] = None, path: str = DEFAULT_PATH) -> List[Dict]:
    return AssetRegistry(path).list(kind=kind)

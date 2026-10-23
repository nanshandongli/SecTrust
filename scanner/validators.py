"""
活体验证（scanner/validators.py）—— 让"疑似密钥"不只是正则命中，而是尽量确认有效性。

对应路线 2 的"活体验证"：TruffleHog 之所以强，是因为它会对疑似密钥做实时校验
（如 GitHub token 调 API 验证、AWS key 调 sts:GetCallerIdentity）。本模块提供可插拔的
验证器，默认离线（unverified），开启 LIVE_VALIDATION=1 后做真实 API 校验。

⚠️ 默认关闭真实联网校验：避免对第三方服务造成意外请求；生产启用请在隔离环境、并对
    目标服务速率限制负责。验证只在"已审批"的扫描结果上执行（双闸门仍生效）。
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from abc import ABC, abstractmethod
from typing import Dict


# GitHub PAT / 传统 token 形态
_GH_PAT = re.compile(r"^(ghp_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{82}|gho_|ghu_|ghs_|ghr_)")


class BaseValidator(ABC):
    """验证器抽象：输入疑似密钥与类型，返回 {verdict, detail}。"""

    name = "base"

    @abstractmethod
    def validate(self, secret: str, secret_type: str = "") -> Dict:
        ...


class NoopValidator(BaseValidator):
    """离线默认：不联网，标记 unverified（仅供后续人工/自动策略判断）。"""

    name = "noop"

    def validate(self, secret: str, secret_type: str = "") -> Dict:
        return {"verdict": "unverified", "detail": "离线模式未做活体验证"}


class GitHubTokenValidator(BaseValidator):
    """GitHub token 活体验证：调 api.github.com/user 确认 token 是否有效。

    仅在 LIVE_VALIDATION=1 时真正联网；否则返回 unverified。"""

    name = "github"

    def __init__(self, live: bool = None):
        self.live = live if live is not None else os.getenv("LIVE_VALIDATION") == "1"

    def validate(self, secret: str, secret_type: str = "") -> Dict:
        if not _GH_PAT.match(secret) and secret_type not in ("github", "gh_token"):
            return {"verdict": "unverified", "detail": "非 GitHub token 形态"}
        if not self.live:
            return {"verdict": "unverified", "detail": "LIVE_VALIDATION 未开启"}
        try:
            req = urllib.request.Request(
                "https://api.github.com/user",  # noqa: S310 (GitHub 官方)
                headers={"Authorization": f"Bearer {secret}",
                          "User-Agent": "sectrust-scanner"},
            )
            with urllib.request.urlopen(req, timeout=8) as r:  # noqa: S310
                data = json.loads(r.read())
            login = data.get("login", "?")
            return {"verdict": "valid", "detail": f"有效 GitHub token，属用户 {login}"}
        except urllib.error.HTTPError as e:
            if e.code == 401:
                return {"verdict": "invalid", "detail": "token 无效/已吊销（401）"}
            return {"verdict": "error", "detail": f"HTTP {e.code}"}
        except Exception as e:  # noqa: BLE001
            return {"verdict": "error", "detail": str(e)[:200]}


# 默认验证器注册表（按类型选择）
_VALIDATORS = {
    "github": GitHubTokenValidator(),
}
_DEFAULT = NoopValidator()


def validate_secret(secret: str, secret_type: str = "") -> Dict:
    v = _VALIDATORS.get(secret_type, _DEFAULT)
    return v.validate(secret, secret_type)


def validate_findings(findings: list, type_key: str = "secret_type") -> list:
    """对一批发现批量跑活体验证，给每条附加 validation 字段。"""
    out = []
    for f in findings:
        secret = f.get("secret") or f.get("match") or ""
        stype = f.get(type_key, "")
        f = dict(f)
        if secret:
            f["validation"] = validate_secret(secret, stype)
        out.append(f)
    return out


if __name__ == "__main__":
    print(validate_secret("ghp_" + "a" * 36, "github"))  # 形态对但无效（离线 unverified）
    print(validate_secret("not-a-token", "github"))

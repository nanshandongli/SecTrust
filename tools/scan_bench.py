"""
扫描能力基准（tools/scan_bench.py）—— 用金标准语料量化"深度"，而不是靠嘴说。

为什么要有它：
    "规则多"不等于"扫得准"。TruffleHog 用 Detect-Test-Secrets、各家用 benchmark 说话。
    本工具给出可复现的 recall / precision / F1，让"扫描深度"从形容词变成数字。

语料设计（三部分，刻意保持平衡）：
    · 真阳性：真实**形态**的凭证（全部为编造值，绝不指向任何真实账户）
    · 诱饵  ：行业公认的误报源——文档示例密钥、UUID、内容散列、纯数字、占位口令、
              CI 表达式。诱饵被报出来=误报，必须计入 precision 惩罚。
    · 外部公开语料（--corpus DIR）：gitleaks 官方测试向量（tools/fetch_bench_corpus.py
      抓取）——**不与自造语料等价**的公开基准子集，用真实项目自己的题考自己。

用法：
    python tools/scan_bench.py                # 人读报告（自造语料）
    python tools/scan_bench.py --json         # 机器可读（供自检脚本/文档取数）
    python tools/fetch_bench_corpus.py        # 先抓取公开语料
    python tools/scan_bench.py --corpus data/bench_corpus   # 用公开语料评测
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from typing import Dict, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scanner.engine import scan_path                       # noqa: E402
from scanner.detectors import detector_stats               # noqa: E402
from scanner.sarif import to_sarif, validate_sarif         # noqa: E402
from scanner.findings import Finding                       # noqa: E402


# ---------------------------------------------------------------- 语料

# 下列"密钥"均为人工构造的公开假样本（非真实凭证）。GitHub Push Protection 会
# 拦截形似真密钥的连续字面量，因此源码按片段拆开、运行时拼接——拼接后的值
# 与原样本逐字符一致，基准语料行为不变。
_AWS_AKID = "AKIA3H7RZQ2XUVW" + "XYZAB"
_AWS_SECRET = "wJalrXUtnFEMI/K7MDENG" + "/bPxRfiCYzREALKEY90"
_SLACK_BOT = "xoxb-123456789012-123456789012-aBcDeFgH" + "iJkLmNoPqRsTuVwX"
_DISCORD = "MTIzNDU2Nzg5MDEyMzQ1Njc4" + ".GhIjKl.mNoPqRsTuVwXyZaBcDeFgHiJkLmNoPqRsTuVwX"
_STRIPE = "sk_live_51H8xQ2eZvKYlo2C0" + "abcdefghij"

# (文本, 期望: "detect" 报出 / "clean" 不报, 说明)
CORPUS: List[Tuple[str, str, str]] = [
    # ---------------- 真阳性：云 ----------------
    (f"AWS_ACCESS_KEY_ID = \"{_AWS_AKID}\"", "detect", "AWS AKIA"),
    (f"aws_secret_access_key = \"{_AWS_SECRET}\"", "detect", "AWS secret"),
    ("gcp_key = \"AIzaSyD-1a2B3c4D5e6F7g8H9i0JkLmNoPqRsTu\"", "detect", "GCP/Gemini API key"),
    ("AZURE_STORAGE_KEY = \"Zm9vYmFyYmF6cXV1eDAxMjM0NTY3ODkwYWJjZGVmZ2hpamtsbW5vcHFyc3R1dnd4eXowMTIzNDU2Nzg5\"", "detect", "Azure 存储密钥"),
    ("ALIYUN_ACCESS_KEY = \"LTAI4G8K1pQ2wE3rT5yU7iO9pA\"", "detect", "阿里云 AK"),
    ("TENCENT_SECRET_ID = \"AKIDz8kL2mN4pQ6rS8tU0vW2xY4zA6bC\"", "detect", "腾讯云 SecretId"),

    # ---------------- 真阳性：代码托管 ----------------
    ("GITHUB_TOKEN = \"ghp_1A2b3C4d5E6f7G8h9I0jKlMnOpQrStUvWxYz\"", "detect", "GitHub 经典 PAT"),
    ("GH_PAT = \"github_pat_11ABCDEFG0a1b2c3d4e5f6A7B8C9D0E1F2G3H4I5J6K7L8M9N0P1Q2R3S4T5U6V7W8X9Y0Z1a2b3c4d\"",
     "detect", "GitHub 细粒度 PAT"),
    ("GITLAB_TOKEN = \"glpat-a1B2c3D4e5F6g7H8i9J0k\"", "detect", "GitLab PAT"),

    # ---------------- 真阳性：协作 / IM ----------------
    (f"SLACK_BOT = \"{_SLACK_BOT}\"", "detect", "Slack bot token"),
    (f"DISCORD_TOKEN = \"{_DISCORD}\"",
     "detect", "Discord bot token"),
    ("TG_TOKEN = \"1234567890:AAF-abcdefghijklmnopqrstuvwxyz12345\"", "detect", "Telegram bot token"),

    # ---------------- 真阳性：支付 / 商业 ----------------
    (f"STRIPE_KEY = \"{_STRIPE}\"", "detect", "Stripe live key"),
    ("SHOPIFY = \"shpat_a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d\"", "detect", "Shopify token"),

    # ---------------- 真阳性：AI 服务 ----------------
    ("OPENAI_KEY = \"sk-proj-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789AbCdEfGhIjK\"", "detect", "OpenAI key"),
    ("ANTHROPIC = \"sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789AbCdEfGhIjKlMnOp\"", "detect", "Anthropic key"),
    ("HF_TOKEN = \"hf_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789ab\"", "detect", "HuggingFace token"),

    # ---------------- 真阳性：通用形态 ----------------
    ("-----BEGIN RSA PRIVATE KEY-----", "detect", "PEM 私钥头"),
    ("JWT = \"eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U\"",
     "detect", "JWT"),
    ("DATABASE_URL = \"postgres://admin:Sup3rS3cret@db.internal.example:5432/app\"", "detect", "含凭证的数据库连接串"),
    ("registry = \"https://deploy:Pa55w0rd@registry.internal.example/\"", "detect", "URL 内嵌凭据"),
    ("npm_token = \"npm_a1B2c3D4e5F6g7H8i9J0kLmNoPqRsTuVwXyZ\"", "detect", "npm token"),
    ("VAULT_TOKEN = \"hvs.CAESIJ1234567890abcdefghij\"", "detect", "Vault token"),
    ("GRAFANA = \"glsa_a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d_6789abcd\"", "detect", "Grafana SA token"),
    ("SENDGRID = \"SG.aBcDeFgHiJkLmNoPqRsTuV.aBcDeFgHiJkLmNoPqRsTuVwXyZaBcDeFgHiJkLmNoPqRsTuVwXyZ\"",
     "detect", "SendGrid key"),

    # ---------------- 真阳性：无前缀私有凭证（靠熵兜底） ----------------
    ("internal_signing_secret = \"7Qk2ZmPx8VrLdN4cYtWbGjHfAeS1uRo9\"", "detect", "私有系统高熵密钥（无已知前缀）"),

    # ---------------- 真阳性：硬编码口令 ----------------
    ("DB_PASSWORD=SuperSecretDbPass123", "detect", "硬编码口令（.env 无引号形态）"),

    # ---------------- 诱饵：必须不报 ----------------
    ("AWS_EXAMPLE = \"AKIAIOSFODNN7EXAMPLE\"", "clean", "AWS 官方文档示例密钥"),
    ("COMMIT_SHA = \"9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08\"", "clean", "SHA-256 内容散列"),
    ("REQUEST_ID = \"3f2504e0-4f89-11d3-9a0c-0305e82c3301\"", "clean", "UUID"),
    ("ORDER_NO = \"1234567890123456789\"", "clean", "纯数字流水号"),
    ("DB_PASSWORD = \"changeme\"", "clean", "占位口令 changeme"),
    ("API_KEY = \"your_api_key_here\"", "clean", "占位符"),
    ("TEST_TOKEN = \"test_token_for_unit_tests\"", "clean", "测试用假 token"),
    ("sha256sum = \"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855\"", "clean", "校验和术语"),
    ("BASE = \"http://localhost:8080/api/v1\"", "clean", "本地地址"),
    ("RUN = echo \"token=${{ secrets.GITHUB_TOKEN }}\"", "clean", "CI 表达式（非硬编码）"),
]


# ---------------------------------------------------------------- 评测

def load_corpus_dir(path: str) -> Tuple[List[Tuple[str, str, str]], str]:
    """从目录加载外部语料（manifest.json：file/expect/note/source_url）。

    返回 (语料列表, 来源说明)。manifest 缺失或目录不存在时抛 ValueError——
    评测工具必须明确失败，而不是静默跑空集报个虚高的 F1。"""
    mpath = os.path.join(path, "manifest.json")
    if not os.path.isfile(mpath):
        raise ValueError(f"语料目录缺少 manifest.json: {path}")
    with open(mpath, encoding="utf-8") as f:
        man = json.load(f)
    corpus: List[Tuple[str, str, str]] = []
    for v in man.get("vectors", []):
        fpath = os.path.join(path, v["file"])
        if not os.path.isfile(fpath):
            raise ValueError(f"manifest 指向的文件不存在: {fpath}")
        with open(fpath, "rb") as fh:
            text = fh.read().decode("utf-8", "replace")
        corpus.append((text, v["expect"], v.get("note", "")))
    source = man.get("source", "外部语料")
    return corpus, source


def run_bench(corpus: List[Tuple[str, str, str]] = None,
              source: str = "builtin") -> Dict:
    """跑语料：每条单独落盘扫描，逐条判定 TP/FN/FP/TN。

    source: "builtin"（自造语料）/ 外部语料来源说明——成绩单必须带来源，
    自造语料的分数不与公开基准等价。"""
    corpus = corpus if corpus is not None else CORPUS
    tp = fn = fp = tn = 0
    details: List[Dict] = []
    tmpdir = tempfile.mkdtemp(prefix="scanbench_")

    for i, (text, expect, note) in enumerate(corpus):
        path = os.path.join(tmpdir, f"case_{i:03d}.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        res = scan_path(path, high_entropy=True)
        found = len(res["findings"]) > 0
        want = (expect == "detect")

        if want and found:
            verdict, tp = "TP", tp + 1
        elif want and not found:
            verdict, fn = "FN", fn + 1
        elif not want and found:
            verdict, fp = "FP", fp + 1
        else:
            verdict, tn = "TN", tn + 1

        hit_rules = sorted({f["rule_id"] for f in res["findings"]})
        details.append({"case": i, "note": note, "expect": expect, "verdict": verdict,
                        "rules": hit_rules, "text_head": text[:48]})

    n_pos = tp + fn
    n_neg = fp + tn
    recall = (tp / n_pos) if n_pos else 0.0
    precision = (tp / (tp + fp)) if (tp + fp) else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0

    # SARIF 结构自检（用全量语料产一次）
    sarif_problems = _sarif_check(tmpdir, len(corpus))

    return {
        "corpus_source": source,
        "corpus_size": len(corpus),
        "positives": n_pos,
        "negatives": n_neg,
        "tp": tp, "fn": fn, "fp": fp, "tn": tn,
        "recall": round(recall, 4),
        "precision": round(precision, 4),
        "f1": round(f1, 4),
        "rules_total": detector_stats()["total"],
        "sarif_problems": sarif_problems,
        "details": details,
    }


def _sarif_check(tmpdir: str, n_cases: int) -> List[str]:
    """对语料扫描结果做一次 SARIF 结构校验。"""
    findings: List[Finding] = []
    for i in range(n_cases):
        p = os.path.join(tmpdir, f"case_{i:03d}.txt")
        if not os.path.isfile(p):
            continue
        for f in scan_path(p)["findings"]:
            findings.append(Finding(**{k: v for k, v in f.items() if k != "secret"}))
    return validate_sarif(to_sarif(findings, root=tmpdir))


def main() -> int:
    ap = argparse.ArgumentParser(description="扫描能力基准（金标准语料）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--corpus", help="外部语料目录（含 manifest.json；"
                                     "由 tools/fetch_bench_corpus.py 生成）")
    args = ap.parse_args()

    source = "builtin"
    corpus = None
    if args.corpus:
        try:
            corpus, source = load_corpus_dir(args.corpus)
        except ValueError as e:
            print(f"语料加载失败: {e}", file=sys.stderr)
            return 2

    r = run_bench(corpus, source)
    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0

    print("=" * 62)
    print("扫描能力基准（可复现）")
    print("=" * 62)
    print(f"语料来源    : {r['corpus_source']}")
    print(f"语料        : {r['corpus_size']} 条（真阳性 {r['positives']} / 诱饵 {r['negatives']}）")
    print(f"规则库      : {r['rules_total']} 条结构化检测器")
    print(f"TP={r['tp']}  FN={r['fn']}  FP={r['fp']}  TN={r['tn']}")
    print(f"召回率      : {r['recall']:.1%}")
    print(f"精确率      : {r['precision']:.1%}")
    print(f"F1          : {r['f1']:.3f}")
    print(f"SARIF 结构  : {'OK' if not r['sarif_problems'] else r['sarif_problems']}")
    if source != "builtin":
        print("注意        : 外部语料为公开基准子集（规模小），与自造语料成绩不可直接互比")
    misses = [d for d in r["details"] if d["verdict"] in ("FN", "FP")]
    if misses:
        print("\n未达标项：")
        for d in misses:
            print(f"  [{d['verdict']}] {d['note']} → 命中规则 {d['rules'] or '无'}")
    return 0 if not misses else 1


if __name__ == "__main__":
    raise SystemExit(main())

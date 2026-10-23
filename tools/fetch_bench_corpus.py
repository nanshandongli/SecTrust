"""
tools/fetch_bench_corpus.py —— 抓取**公开基准语料**（gitleaks 官方测试向量）。

为什么要有它：tools/scan_bench.py 的内置语料是**自造**的——自造语料的高分
只证明"能过自己出的题"，不与公开基准等价。本工具从 gitleaks 官方仓库
（github.com/gitleaks/gitleaks，Apache-2.0）拉取它自己的测试向量，
落到 data/bench_corpus/ 并生成 manifest.json，之后 scan_bench --corpus
即可用**外部公开语料**评测——那才是与 TruffleHog/gitleaks 可比的成绩。

诚实的边界：
- 语料**来自 gitleaks 的测试目录**，但期望值（detect/clean）由本工具按
  gitleaks 自己的 ignore/expected 文件推断标注，并非官方发布的标注集；
- 规模很小（个位数向量），是"公开基准子集"，不是完整基准；
- 抓取需要网络；失败时保留原有自造语料结果，并在报告里如实标注来源。

用法：
    python tools/fetch_bench_corpus.py            # 抓取 + 写 manifest
    python tools/scan_bench.py --corpus data/bench_corpus --json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

OUT_DIR = os.path.join(REPO_ROOT, "data", "bench_corpus")
RAW = "https://raw.githubusercontent.com/gitleaks/gitleaks/master"

# (仓库内路径, 期望, 说明)
# 期望值依据：gitleaks 自带的 .gitleaksignore/expected 文件指向这些文件的具体行，
# 即官方预期"这里有发现"。README 类文件官方用于无发现场景。
VECTORS = [
    ("testdata/repos/nogit/api.go", "detect",
     "gitleaks 官方测试向量：第 20 行 awsToken := \"AKIALALEMEL33243OLIA\"（其 .gitleaksignore 明确指向 aws-access-key:20）"),
    ("testdata/repos/nogit/.env.prod", "detect",
     "gitleaks 官方测试向量：DB_PASSWORD=8ae31cacf141669ddfb5da（高熵口令，gitleaks 在该目录预期有发现）"),
    ("testdata/repos/small/README.md", "clean",
     "gitleaks 官方测试向量：纯说明文档，无凭证"),
]


def fetch(url: str, timeout: float = 30, retries: int = 3) -> bytes:
    """抓取 URL 内容。curl 优先（本机 urllib 对 raw.githubusercontent 有 TLS 握手问题，
    实测 curl 正常），无 curl 再退回 urllib。网络抖动时有限重试（退避 2s/4s）。"""
    import shutil
    import subprocess
    import time as _t
    last: Exception = OSError("未尝试")
    for attempt in range(retries):
        try:
            if shutil.which("curl"):
                r = subprocess.run(
                    ["curl", "-sSL", "--max-time", str(int(timeout)), url],
                    capture_output=True, timeout=timeout + 10)
                if r.returncode == 0 and r.stdout:
                    return r.stdout
                last = OSError(f"curl: {r.stderr.decode('utf-8', 'replace')[:120]}")
            else:
                with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310
                    return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
        if attempt < retries - 1:
            _t.sleep(2 * (attempt + 1))
    raise last


def verify() -> int:
    """离线校验：本地语料文件与 manifest 的 sha256 是否一致（不联网）。

    用于网络不可达时确认"手里的语料就是当初抓下来的那批"，防静默漂移。
    """
    mpath = os.path.join(OUT_DIR, "manifest.json")
    if not os.path.isfile(mpath):
        print(f"[fail] 缺少 manifest: {mpath}（先联网抓取）")
        return 1
    with open(mpath, encoding="utf-8") as f:
        man = json.load(f)
    drift = 0
    for v in man.get("vectors", []):
        fpath = os.path.join(OUT_DIR, v["file"])
        if not os.path.isfile(fpath):
            print(f"[fail] 缺文件: {v['file']}")
            drift += 1
            continue
        actual = hashlib.sha256(open(fpath, "rb").read()).hexdigest()
        if actual == v.get("sha256"):
            print(f"[ok] {v['file']} 与 manifest 一致")
        else:
            print(f"[drift] {v['file']}: manifest={v.get('sha256', '')[:16]}… "
                  f"actual={actual[:16]}…")
            drift += 1
    print(f"\n校验完成：{len(man.get('vectors', []))} 个向量，{drift} 个漂移")
    return 0 if drift == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="抓取/校验公开基准语料（gitleaks 测试向量）")
    ap.add_argument("--verify", action="store_true",
                    help="离线校验本地语料与 manifest 的 sha256 一致性（不联网）")
    args = ap.parse_args()
    if args.verify:
        return verify()

    os.makedirs(OUT_DIR, exist_ok=True)
    manifest = {
        "source": "github.com/gitleaks/gitleaks (Apache-2.0) 官方测试向量",
        "note": ("外部公开语料子集；期望值由 gitleaks 自带 ignore/expected 推断标注，"
                 "非官方标注集；规模小，不等价完整公开基准"),
        "vectors": [],
    }
    ok = fail = 0
    for i, (path, expect, note) in enumerate(VECTORS):
        url = f"{RAW}/{path}"
        try:
            data = fetch(url)
        except Exception as e:  # noqa: BLE001
            print(f"[fail] {path}: {e}")
            fail += 1
            continue
        fname = f"case_{i:03d}_{os.path.basename(path)}"
        fpath = os.path.join(OUT_DIR, fname)
        with open(fpath, "wb") as f:
            f.write(data)
        manifest["vectors"].append({
            "file": fname, "expect": expect, "note": note,
            "source_url": url,
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
        })
        print(f"[ok] {path} -> {fname} ({len(data)}B, expect={expect})")
        ok += 1
    with open(os.path.join(OUT_DIR, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print(f"\n完成：{ok} 个向量，{fail} 个失败。语料目录: {OUT_DIR}")
    print("评测: python tools/scan_bench.py --corpus " + OUT_DIR + " --json")
    return 0 if ok and not fail else 1


if __name__ == "__main__":
    raise SystemExit(main())

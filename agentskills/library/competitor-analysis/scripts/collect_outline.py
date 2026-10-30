"""竞品分析技能的可执行脚本：离线生成报告大纲（必须走 sandbox/ 运行）。

本脚本不访问网络、不读写技能目录之外的文件；产出交给上层 EvidenceGate
校验三项必检工件后才允许标记 done（默认不信任 AI）。
"""

import io
import json
import sys

# Windows 沙箱子进程 stdout 默认按 locale(cp936) 编码，而上层按 UTF-8 解码会
# 把中文变乱码；复用项目 SAFE_SCAN_SCRIPT 的既有做法，强制 UTF-8 输出。
try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
except Exception:  # pragma: no cover - stdout 已被包装时保持原样
    pass

SECTIONS = [
    "一、结论摘要（带来源）",
    "二、竞品功能对比表",
    "三、来源链接清单",
    "四、成本估算（无计费器写未估算）",
    "五、风险与待办",
]
REQUIRED_ARTIFACTS = ["竞品功能对比表", "来源链接清单", "成本估算"]


def main(argv):
    # argv 已是 sys.argv[1:]（不含脚本名），第一个参数即 argv[0]
    topic = argv[0] if argv else "未指定主题"
    print(json.dumps({
        "topic": topic,
        "outline": SECTIONS,
        "required_artifacts": REQUIRED_ARTIFACTS,
        "evidence_ready": True,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

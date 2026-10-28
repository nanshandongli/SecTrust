"""
tools/emit_files.py —— 手动触发落盘的命令行入口。

为什么需要它：
    daemon 里的 `op=write_files` 是**流水线内**的自动落盘（software_dev 计划跑到
    write_code 工序时触发）。但你常常手上已经有一份模型产出的 markdown（或一份
    JSON 清单），只想把它落成文件看看——为此跑一整条流水线太重了。这个脚本就是
    那条"手动落盘"的捷径。

用法：
    # 把模型产出里的 fenced 代码块落成文件（文件名从 title=/# file: 推断）
    python tools/emit_files.py --from-file 产出.md --target ./myrepo

    # 从 JSON 清单落盘（[{"path": "a.py", "content": "..."}]）
    python tools/emit_files.py --from-json files.json --target ./myrepo

    # 从 stdin 读（方便管道接上一个生成步骤）
    cat 产出.md | python tools/emit_files.py --stdin --target ./myrepo

    # 允许覆盖已存在的文件（默认拒覆盖，防手滑）
    python tools/emit_files.py --from-file 产出.md --target ./myrepo --overwrite

    # 让脚本消费：只输出 JSON
    python tools/emit_files.py --from-file 产出.md --target ./myrepo --json

退出码：
    0  落盘成功，且密钥闸门通过
    1  落盘成功，但**扫描命中高危**（文件已写入，不回滚——由人决定怎么处理）
    2  用法/输入错误，或全部被拒写（磁盘上不会出现任何文件）

诚实边界（务必）：
    - 只写文件，**不执行任何命令、不做 git 操作**（commit/push 永远归人）；
    - 扫描是"检测"不是"修复"：命中只报告，绝不擅自改你的代码；
    - 命中高危时文件**已经落盘**，脚本如实报出来并退 1，**绝不偷偷删**——
      静态回滚比写错更危险，删不删是你决定的事；
    - `--target` 必须是已存在的目录（要新建请显式加 `--create`），
      防止手滑在陌生路径建出一堆目录。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from execution.writer import (extract_code_blocks, normalize_files,       # noqa: E402
                              write_files, summary_text)
from execution.workspace import BotWorkspace                              # noqa: E402


def _is_filesystem_root(path: str) -> bool:
    """是否指向文件系统根——写到那里基本都是手滑。

    Windows 上 abspath("/") 会得到当前盘符根（如 C:\\），abspath("C:/") 同样；
    所以两个判据都要有：等于自身 dirname（Unix 根/盘符根）+ 盘符根正则（含 / 形态）。
    """
    norm = os.path.abspath(path)
    if norm == os.path.dirname(norm):
        return True
    return bool(re.match(r"^[A-Za-z]:[\\/]?$", norm))


def _read_source(args) -> tuple:
    """返回 (files_or_none, extract_text_or_none, error)。三选一输入。"""
    given = [bool(args.from_file), bool(args.from_json), bool(args.stdin)]
    if sum(given) == 0:
        return None, None, "需要指定输入来源：--from-file / --from-json / --stdin 之一"
    if sum(given) > 1:
        return None, None, "输入来源只能指定一个（--from-file / --from-json / --stdin）"

    if args.from_file:
        p = args.from_file
        if not os.path.isfile(p):
            return None, None, "文件不存在：%s" % p
        try:
            with open(p, encoding="utf-8") as f:
                return None, f.read(), None
        except (OSError, UnicodeDecodeError) as e:
            return None, None, "读取失败：%s" % e

    if args.from_json:
        p = args.from_json
        if not os.path.isfile(p):
            return None, None, "文件不存在：%s" % p
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            return None, None, "JSON 解析失败：%s" % e
        if isinstance(data, dict) and isinstance(data.get("files"), list):
            data = data["files"]
        if not isinstance(data, list):
            return None, None, "JSON 顶层必须是数组，或含 files 数组的对象"
        return data, None, None

    # --stdin
    try:
        text = sys.stdin.read()
    except OSError as e:
        return None, None, "读取 stdin 失败：%s" % e
    if not text.strip():
        return None, None, "stdin 为空"
    return None, text, None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="emit_files.py",
        description="把模型产出的代码（或 JSON 清单）真正落盘成文件，"
                    "落盘后跑密钥扫描；命中高危如实报告并退 1。")
    ap.add_argument("--from-file", metavar="PATH",
                    help="模型产出的 markdown 原文（自动提取 fenced 代码块）")
    ap.add_argument("--from-json", metavar="PATH",
                    help="显式清单 JSON：[{\"path\":\"a.py\",\"content\":\"...\"}]")
    ap.add_argument("--stdin", action="store_true",
                    help="从 stdin 读取 markdown 原文")
    ap.add_argument("--target", metavar="DIR", required=True,
                    help="落盘根目录（必须是已存在目录，除非加 --create）")
    ap.add_argument("--create", action="store_true",
                    help="--target 不存在时创建它")
    ap.add_argument("--overwrite", action="store_true",
                    help="允许覆盖已存在文件（默认拒覆盖）")
    ap.add_argument("--no-scan", action="store_true",
                    help="落盘后不跑密钥扫描（不推荐）")
    ap.add_argument("--json", action="store_true",
                    help="只输出 JSON 结果，供脚本消费")
    ap.add_argument("--bot-id", default="cli-emit",
                    help="临时工作区的 bot id（默认 cli-emit）")
    args = ap.parse_args(argv)

    # Windows 控制台可能是 GBK，中文摘要会炸
    try:
        sys.stdout.reconfigure(encoding="utf-8")    # py3.7+
    except Exception:                                # noqa: BLE001
        pass

    # ---- 目标目录安全 ----
    if _is_filesystem_root(args.target):
        print("拒绝：--target 指向文件系统根目录（%s）——这基本是手滑" % args.target)
        return 2
    target = os.path.abspath(args.target)
    if not os.path.isdir(target):
        if not args.create:
            print("错误：--target 不是已存在的目录：%s" % target)
            print("      确认无误后可加 --create 让脚本创建它")
            return 2
        try:
            os.makedirs(target, exist_ok=True)
        except OSError as e:
            print("错误：无法创建目录 %s：%s" % (target, e))
            return 2

    # ---- 读输入 ----
    files, extract, err = _read_source(args)
    if err:
        print("错误：%s" % err)
        return 2

    blocked_pre = []
    if files is not None:
        items, blocked_pre = normalize_files(files)
    else:
        blocks = extract_code_blocks(extract)
        if not blocks:
            print("错误：原文里没有找到 fenced 代码块（模型可能只给了说明没给代码）")
            return 2
        items, blocked_pre = normalize_files(blocks)
    if not items:
        print("错误：没有可落盘的合法文件")
        for b in blocked_pre:
            print("  拒写 %s：%s" % (b.get("path") or "(未知)", b.get("reason") or ""))
        return 2

    # 落盘前先把绝对路径摆出来：手滑传错目录时，这是最后一道看得见的提醒
    if not args.json:
        print("目标目录：%s（将写入 %d 个文件）" % (target, len(items)))

    # ---- 落盘 ----
    ws_root = tempfile.mkdtemp(prefix="emit-files-")
    try:
        ws = BotWorkspace(args.bot_id, root=ws_root)
        res = write_files(ws, items, blocked=blocked_pre, root=target,
                          allow_outside=True,          # --target 即显式授权
                          overwrite=bool(args.overwrite),
                          scan=not args.no_scan)
    except Exception as e:                              # noqa: BLE001
        print("落盘失败：%s" % e)
        return 2
    finally:
        import shutil
        shutil.rmtree(ws_root, ignore_errors=True)

    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=1, default=str))
    else:
        print(summary_text(res))

    if not res.get("written"):
        print("\n结果：全部被拒写，磁盘上没有写入任何文件")
        return 2

    scan = res.get("scan") or {}
    if scan.get("should_fail"):
        print("\n⚠ 密钥闸门触发：%s" % (scan.get("fail_reason") or "命中高危凭证"))
        print("  文件**已写入**磁盘（脚本不会偷偷删）。请人工确认后决定保留或回滚。")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

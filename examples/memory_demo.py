"""
轻量长期记忆演示（P2）

演示要点：
1. 写入三类记忆：用户偏好 / 常用指令模板 / 常用 repo 路径；
   其中「代码审查用 Qwen-Max」是跨会话记忆的典型用例。
2. 模拟「下一次会话」：重新打开同一 SQLite 文件，原进程已关闭，
   新实例依然能 recall 到上次写入的内容 —— 证明跨会话持久化成立。
3. 演示 search 模糊回忆 与 top_used「常用」排名（访问计数随 recall 累积）。

零依赖、离线可跑：python examples/memory_demo.py
"""

import os
import sys
import tempfile

# 允许以 `python examples/memory_demo.py` 直接运行：把仓库根加入导入路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory import (
    CAT_COMMAND,
    CAT_PREFERENCE,
    CAT_REPO,
    MemoryStore,
)


def main():
    # 用临时库避免污染项目 data/memory.db；真实使用直接 MemoryStore() 即可
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "memory_demo.db")

    print("=" * 60)
    print("【第 1 步】会话 A：写入记忆")
    print("=" * 60)
    mem = MemoryStore(db_path=db)

    # —— 用户偏好：跨会话记住「这个用户喜欢用 Qwen-Max 做代码审查」——
    mem.remember_preference("code_review_model", "Qwen-Max", note="用户偏好的代码审查模型")
    mem.remember_preference("preferred_lang", "Python", note="用户主力语言")

    # —— 常用指令模板（带占位符）——
    mem.remember_command(
        "scan_repo",
        "python mcp_server.py && python examples/template_scan_demo.py --backend local --auto-approve",
        note="一键合规扫描本地仓库",
    )
    mem.remember_command(
        "run_tests", "python -m unittest discover -s tests -v", note="跑全套回归测试"
    )

    # —— 常用 repo 路径 ——
    mem.remember_repo("this_project", os.getcwd(), note="当前项目根")
    mem.remember_repo("sample_repo", os.path.join(os.getcwd(), "examples", "sample_repo"))

    print("写入完成，各分类条数：", mem.stats())

    # 模拟多次使用「常用指令」，让 top_used 有区分度
    for _ in range(3):
        mem.recall_command("run_tests")  # 访问计数 +3
    mem.recall_command("scan_repo")      # 访问计数 +1

    mem.close()

    print()
    print("=" * 60)
    print("【第 2 步】会话 B：重新打开同一文件（模拟下次启动/跨会话）")
    print("=" * 60)
    mem2 = MemoryStore(db_path=db)

    model = mem2.recall_preference("code_review_model")
    print("跨会话读到 code_review_model =>", model)
    assert model == "Qwen-Max", "跨会话记忆失效！"
    print("✅ 跨会话持久化成立：本次会话无需重新写入即可记得上次偏好。")

    print()
    print("—— 模糊回忆：搜索 'repo' 相关记忆 ——")
    for it in mem2.search("repo"):
        print(f"  [{it['category']}] {it['key']} = {it['value']}  (note={it['note']})")

    print()
    print("—— 常用指令模板 Top（按访问频次） ——")
    for it in mem2.top_used(CAT_COMMAND, n=5):
        print(f"  {it['key']:12s} 访问 {it['access_count']} 次 -> {it['value']}")

    print()
    print("—— 全部偏好 ——")
    for it in mem2.list_category(CAT_PREFERENCE):
        print(f"  {it['key']:18s} = {it['value']}")

    mem2.close()
    print()
    print("演示结束。真实使用：直接 `from memory import MemoryStore` 并在进程退出前 close()。")
    print("若通过 MCP 入口，外部 agent 调 memory_set / memory_get 读写的是同一份 data/memory.db。")


if __name__ == "__main__":
    main()

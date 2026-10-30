#!/usr/bin/env python3
"""
bench_rerank_latency.py —— 实测 LLM 语义重排的端到端延迟成本。

离线环境真实 LLM 可能返回 429（fail-fast 降级，会掩盖真实推理耗时），
所以这里用「可控延时 FakeLLM」模拟真实推理延迟，量化：
  1. heuristic-only 路径基线耗时
  2. LLM-path（含 N 毫秒推理延时）额外耗时
  3. 各 query 的候选集数量分布（决定阈值是否合理）

用法（无需联网、零依赖）：
  python bench_rerank_latency.py            # 默认延时 300ms
  python bench_rerank_latency.py --delay 800
"""
import argparse
import os
import shutil
import sys
import tempfile
import time
from typing import Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agentskills.skill_md import StandardSkillLibrary
from agentskills.router import SkillRouter
from agentskills.state import SkillStateStore


# ---- 贴近真实的技能库（18 个，覆盖常见 dev/product/ops 任务，描述共享关键词）----
SKILLS = [
    ("disk-cleanup", "磁盘释放工具：清理电脑垃圾文件、释放磁盘空间、系统优化瘦身。"),
    ("competitor-report", "竞品分析报告：梳理竞争对手功能对比、输出竞品矩阵与调研文稿。"),
    ("weekly-report", "周报生成：汇总本周工作进展、自动生成格式化的周报文档。"),
    ("deploy-server", "服务器部署：把应用部署到远程服务器、配置 nginx、上线发布。"),
    ("fix-bug", "Bug 修复：定位代码缺陷、分析堆栈、提出修复方案并验证。"),
    ("code-review", "代码评审：审查 Pull Request、发现潜在缺陷、给出改进建议。"),
    ("write-email", "邮件撰写：根据要点起草商务邮件、润色措辞、生成发送草稿。"),
    ("meeting-summary", "会议纪要：整理会议录音转写、提炼决议与待办事项。"),
    ("data-analysis", "数据分析：清洗数据集、做统计描述、输出可视化图表。"),
    ("sql-query", "SQL 查询：根据自然语言需求生成查询语句、优化慢查询。"),
    ("api-doc", "接口文档：自动生成 OpenAPI 文档、补充示例与字段说明。"),
    ("unit-test", "单元测试：为函数编写 pytest 用例、提升覆盖率、mock 依赖。"),
    ("translate", "文本翻译：中英互译、保留术语一致性、适配本地化语气。"),
    ("ppt-gen", "PPT 生成：根据大纲制作演示文稿、排版美化、配图建议。"),
    ("image-gen", "文生图：根据提示词生成配图、设定风格与构图。"),
    ("search-web", "联网搜索：实时检索最新资讯、聚合多来源结果并标注出处。"),
    ("summarize-doc", "文档摘要：长文压缩、提取核心论点、生成结构化摘要。"),
    ("schedule-plan", "日程规划：排布任务时间线、估算工期、给出甘特建议。"),
]


class FakeLLM:
    """可控延时 LLM：模拟真实推理延迟，返回候选原序（纯测开销，不做语义重排）。"""

    def __init__(self, delay: float):
        self.delay = delay

    def complete(self, question, context="", history=None, model=None):
        time.sleep(self.delay)  # 模拟真实推理耗时
        # 从 prompt 里抠出候选名，按原序回传（等价于"LLM 没改顺序"）
        import re, json
        m = re.search(r"候选技能.*?：\n(.*?)\n\n只返回", question, re.DOTALL)
        names = []
        if m:
            for line in m.group(1).strip().splitlines():
                mm = re.match(r"\d+\.\s*([^：:]+)", line)
                if mm:
                    names.append({"name": mm.group(1).strip(), "reason": "x"})
        return {"answer": json.dumps(names, ensure_ascii=False), "model": "fake"}


def build_lib(size: int = 0) -> str:
    root = tempfile.mkdtemp(prefix="bench_lib_")
    skills = SKILLS[:size] if size else SKILLS
    for name, desc in skills:
        d = os.path.join(root, name)
        os.makedirs(d, exist_ok=True)
        text = ("---\nname: %s\ndescription: %s\nversion: 1.0.0\nlicense: MIT\n"
                "allowed-tools: [web_search, file_read]\nrisk_level: low\n"
                "cost_tier: low\n---\n\n# %s\n\nBODY\n" % (name, desc, name))
        with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as fh:
            fh.write(text)
    return root


QUERIES = [
    "帮我清理一下电脑磁盘空间",                 # 应命中 disk-cleanup（少候选）
    "写一份竞品分析报告",                       # competitor-report（少候选）
    "生成这周的周报",                           # weekly-report
    "把这个服务部署到服务器上",                   # deploy-server
    "帮我修一个登录失败的 bug",                  # fix-bug（可能多候选）
    "评审一下这段代码",                         # code-review
    "起草一封给客户的邮件",                       # write-email
    "整理一下刚才会议的纪要",                     # meeting-summary
    "分析一下这份销售数据",                       # data-analysis
    "把这个需求转成 SQL 查询",                   # sql-query + 多候选
    "生成接口文档",                             # api-doc
    "给这个函数补单元测试",                       # unit-test
    "把这段中文翻译成英文",                       # translate
    "做个汇报用的 PPT",                          # ppt-gen
    "搜一下最新的 AI 新闻",                      # search-web
    "总结一下这篇长文档",                         # summarize-doc
    "规划一下下周的项目排期",                     # schedule-plan
    "帮我把应用上线并修一下部署后的报错",           # deploy + fix（多候选）
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--delay", type=float, default=0.3,
                    help="模拟 LLM 推理延时（秒），默认 0.3")
    ap.add_argument("--threshold", type=int, default=2,
                    help="竞争力阈值：与榜首有竞争力的候选数 < N 跳过 LLM（默认 2；0=始终触发）")
    ap.add_argument("--size", type=int, default=0,
                    help="技能库规模上限（默认 0=全部 18 个；设小如 6 演示阈值跳过）")
    args = ap.parse_args()

    root = build_lib(args.size)
    try:
        lib = StandardSkillLibrary(roots=[root])
        state = SkillStateStore()
        queries = QUERIES

        # 路径 A：纯 heuristic（无 LLM 注入）
        r_h = SkillRouter(library=lib, state=state,
                          llm_rerank_min_competitive=args.threshold)
        # 路径 B：带 LLM（按竞争力阈值决定是否触发）
        fake = FakeLLM(args.delay)
        r_l = SkillRouter(library=lib, state=state, llm=fake,
                          llm_rerank_min_competitive=args.threshold)

        print("=" * 78)
        print("LLM 模拟延时 = %.0fms | 候选阈值 = %s | 技能库 = %d 个"
              % (args.delay * 1000,
                 ("关闭(始终触发)" if args.threshold == 0 else args.threshold),
                 len(SKILLS[:args.size] if args.size else SKILLS)))
        print("=" * 78)
        print("%-30s %5s %9s %9s %6s %5s" %
              ("query", "cands", "heu(ms)", "llm(ms)", "Δ(ms)", "LLM?"))
        print("-" * 78)

        tot_h = tot_l = max_c = 0.0
        n = len(queries)
        fired = 0  # 实际触发 LLM 重排的 query 数
        for q in queries:
            # heuristic
            t0 = time.perf_counter()
            out_h = r_h.select(q, k=5)
            t1 = time.perf_counter()
            # llm path
            t2 = time.perf_counter()
            out_l = r_l.select(q, k=5)
            t3 = time.perf_counter()

            cands = len([c for c in out_h["candidates"] if not c.get("filtered_by")])
            max_c = max(max_c, cands)
            h_ms = (t1 - t0) * 1000
            l_ms = (t3 - t2) * 1000
            tot_h += h_ms
            tot_l += l_ms
            applied = out_l["llm_rerank"].get("applied")
            if applied:
                fired += 1
            print("%-30s %5d %9.2f %9.2f %6.1f %5s" %
                  (q[:29], cands, h_ms, l_ms, l_ms - h_ms,
                   "Y" if applied else "-"))

        print("-" * 78)
        print("均值  heuristic=%.2fms  llm-path=%.2fms  纯 LLM 增量≈%.2fms"
              % (tot_h / n, tot_l / n, (tot_l - tot_h) / n))
        print("触发 LLM 的 query=%d/%d  跳过=%d（阈值=%s）"
              % (fired, n, n - fired,
                 "关" if args.threshold == 0 else args.threshold))
        if fired < n:
            saved = (tot_l - tot_h) / n * (n - fired)
            print("估算因阈值跳过省下的累计延迟≈%.0fms（按 %d 个 query 计）"
                  % (saved, n))
        print("=" * 78)
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()

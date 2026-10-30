---
name: competitor-analysis
description: Use when 用户需要写竞品分析报告、梳理竞品功能对比或输出竞品矩阵时。
version: 1.0.0
license: MIT
allowed-tools: [web_search, file_read, file_write]
risk_level: medium
cost_tier: standard
---

# 竞品分析技能

本技能把「竞品调研」做成可验证、可回放的流水线：先确认竞品名单与对比维度，
再并行搜集公开资料，输出功能对比表，最后生成报告草稿并提交 EvidenceGate
校验。**默认不信任 AI**：任何结论必须带来源链接，缺来源的结论一律标注
「待核实」，不得凭记忆编造具体价格、日期或性能数字。

## 步骤

1. 确认竞品名单与维度：至少列出 3 个竞品、5 个对比维度（价格、功能、生态、
   性能、合规）；名单需用户确认，不自行脑补。
2. 并行搜集公开资料：只用 manifest 的 allowed_domains 白名单内来源，每条结论
   记录 URL 与抓取时间；搜索失败如实标注 unavailable，不伪造资料。
3. 输出功能对比表：使用 assets/report_template.md 的模板骨架，单元格只填
   有来源支撑的事实，空格写「暂无公开资料」。
4. 生成报告草稿：把对比表、来源清单、成本估算三部分拼成一份文档；成本无
   计费器时写「未估算」，不编造数字。
5. 提交 EvidenceGate 校验：产出必须包含「竞品功能对比表」「来源链接清单」
   「成本估算」三项工件，缺任一不允许标记 done——这就是「默认不信任 AI」。

## 渐进式加载资源

- references/sources.md：来源可信度分级与引用格式（L3 按需读取）。
- assets/report_template.md：报告模板骨架（L3 按需读取）。
- scripts/collect_outline.py：离线生成报告大纲的可执行脚本（必须走 sandbox/，
  沙箱外拒绝执行；产出还要过 EvidenceGate）。

## 诚实边界

本技能不自动访问内网数据、不代替人工审批；外发报告、写仓库等高风险动作
需 manifest.requires_approval 或平台审批闸门放行。manifest 的
blocked_patterns 与 governance.content_guard 对齐，用于扫描产出中的密钥泄露。

"""
tests/test_audit_suite.py —— 审计三件套产品化模块回归测试

覆盖：
  · 审批链 SARIF 结构合法性（复用 scanner.sarif.validate_sarif）
  · CSV 导出内容正确性（表头 + 审批行 + 事件行）
  · PDF 导出合法性（%PDF 头 / %%EOF 尾 / startxref；含无字体降级路径）
  · demo() 生成 HTML（三章节齐全）+ 导出样例文件落盘
  · 合成审计链，不污染真实业务库（全部走临时库）
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import tempfile

from audit_suite import (AuditSuite, PdfDoc, approval_to_sarif,
                         find_cjk_font, _TtfFont)
from scanner.sarif import validate_sarif


def _tmp():
    return tempfile.mkdtemp(prefix="audit_suite_test_")


def test_approval_to_sarif_high_risk_is_error():
    chain = {
        "approval": {
            "approval_id": "ap-1", "task_id": "t1", "action": "send",  # 高危
            "status": "approved", "channel": "web", "decided_by": "human",
            "created_at": 1.0, "decided_at": 2.0, "detail": "外发到生产",
        },
        "trace_id": "tr-1",
        "events": [
            {"seq": 1, "kind": "approval_requested", "actor": "u",
             "summary": "请求审批 send", "ts": 1.0},
            {"seq": 2, "kind": "approval_decided", "actor": "human",
             "summary": "send approved", "ts": 2.0},
        ],
    }
    doc = approval_to_sarif(chain)
    problems = validate_sarif(doc)
    assert problems == [], "SARIF 结构应合法: %s" % problems
    results = doc["runs"][0]["results"]
    assert len(results) == 1
    assert results[0]["level"] == "error", "高危动作应标 error"
    assert results[0]["properties"]["risk"] == "high"
    assert results[0]["properties"]["eventCount"] == 2
    rule_ids = {r["id"] for r in doc["runs"][0]["tool"]["driver"]["rules"]}
    assert "approval-decision" in rule_ids


def test_approval_to_sarif_normal_risk_is_warning():
    chain = {
        "approval": {
            "approval_id": "ap-2", "task_id": "t2", "action": "review",  # 普通
            "status": "rejected", "channel": "cli", "decided_by": "human",
            "created_at": 5.0, "decided_at": 9.0, "detail": "复核",
        },
        "trace_id": "tr-2", "events": [],  # 无轨迹上下文也应如实标注
    }
    doc = approval_to_sarif(chain)
    assert validate_sarif(doc) == []
    res = doc["runs"][0]["results"][0]
    assert res["level"] == "warning"
    assert res["properties"]["risk"] == "normal"
    assert res["properties"]["eventCount"] == 0


def _build_synthetic_stores(tmp):
    import audit_suite as A
    trace_db = os.path.join(tmp, "t.db")
    cost_db = os.path.join(tmp, "c.db")
    appr_db = os.path.join(tmp, "a.db")
    ts = A.TraceStore(trace_db)
    cs = A.CostStore(cost_db)
    ac = A.ApprovalCenter(appr_db, tracer=ts)
    tid = ts.start_trace(title="合成流水线", kind="pipeline", started_by="planner")
    t1 = "task_demo_01"
    ts.record(tid, "task_registered", task_id=t1, summary="登记任务")
    ts.record(tid, "task_claimed", task_id=t1, actor="bot", summary="认领")
    aid = ac.request("publish", "发布到 CDN", task_id=t1, channel="web",
                     user_id="ops", trace_id=tid)
    ac.decide(aid, "approve", by="human-ops")
    ts.record(tid, "task_completed", task_id=t1, actor="bot", summary="完成")
    ts.finish(tid, status="completed")
    cs.record_model_call(task_id=t1, trace_id=tid, model="gpt-4o",
                         input_tokens=1200, output_tokens=300, actor="bot")
    cs.record_tool_call(task_id=t1, trace_id=tid, tool="cdn", actor="bot")
    cs.finalize_task(t1, trace_id=tid, role="designer", template="promo",
                     tier="pro", outcome="completed")
    return ts, cs, ac, aid, tid, t1


def test_export_csv_content():
    tmp = _tmp()
    try:
        ts, cs, ac, aid, tid, t1 = _build_synthetic_stores(tmp)
        suite = AuditSuite(trace=ts, cost=cs, approval=ac)
        paths = suite.export_approval(aid, out_dir=tmp, formats=["csv"])
        csv_path = paths["csv"]
        assert paths["found"] is True
        with open(csv_path, encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.reader(fh))
        # 表头 + 审批行 + 2 条审批事件（requested/decided）
        assert len(rows) >= 3
        header = rows[0]
        assert "approval_id" in header and "record_type" in header
        rec_types = [r[1] for r in rows[1:]]
        assert "approval" in rec_types
        assert rec_types.count("event") == 2
        # 审批行动作/风险正确
        approval_row = next(r for r in rows[1:] if r[1] == "approval")
        assert approval_row[3] == "publish"        # action
        assert approval_row[4] == "high"           # risk（publish 高危）
        assert approval_row[5] == "approved"       # status
        ts.close(); cs.close(); ac.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_export_sarif_file_valid():
    tmp = _tmp()
    try:
        ts, cs, ac, aid, tid, t1 = _build_synthetic_stores(tmp)
        suite = AuditSuite(trace=ts, cost=cs, approval=ac)
        paths = suite.export_approval(aid, out_dir=tmp, formats=["sarif"])
        doc = json.load(open(paths["sarif"], encoding="utf-8"))
        assert validate_sarif(doc) == []
        assert doc["version"] == "2.1.0"
        ts.close(); cs.close(); ac.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_export_pdf_valid_head_tail():
    tmp = _tmp()
    try:
        ts, cs, ac, aid, tid, t1 = _build_synthetic_stores(tmp)
        suite = AuditSuite(trace=ts, cost=cs, approval=ac)
        paths = suite.export_approval(aid, out_dir=tmp, formats=["pdf"])
        data = open(paths["pdf"], "rb").read()
        assert data.startswith(b"%PDF-1.4"), "PDF 头非法"
        assert b"%%EOF" in data[-32:], "PDF 尾非法"
        assert b"startxref" in data, "缺少 startxref"
        assert len(data) > 1000, "PDF 体过小"
        ts.close(); cs.close(); ac.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_pdf_fallback_without_font_is_valid():
    """无可用 CJK 字体时降级 Helvetica，仍产出合法 PDF（ASCII 安全）。"""
    doc = PdfDoc(font_path="__nonexistent_font__.ttf")
    doc.add_heading("Approval Chain Audit Report", 15)
    doc.add_paragraph("Fallback path: no CJK font available, ASCII only.")
    doc.add_kv("status", "approved")
    path = os.path.join(_tmp(), "fallback.pdf")
    doc.save(path)
    data = open(path, "rb").read()
    assert data.startswith(b"%PDF-1.4")
    assert b"%%EOF" in data[-32:]
    # 降级路径不应嵌入字体（无 FontFile2）
    assert b"FontFile2" not in data
    os.remove(path)


def test_demo_generates_html_and_exports():
    tmp = _tmp()
    try:
        import audit_suite as A
        r = A.demo(out_dir=os.path.join(tmp, "demo"))
        html_path = r["html"]
        assert os.path.exists(html_path), "HTML 未生成"
        html = open(html_path, encoding="utf-8").read()
        # 三个章节齐备
        assert "Trace 回放" in html
        assert "成本看板" in html
        assert "审批链导出" in html
        # 导出样例文件齐备
        ex = r["exports"]
        assert ex["found"] is True
        for fmt in ("csv", "sarif", "pdf"):
            assert os.path.exists(ex[fmt]), "%s 未生成" % fmt
        # PDF 合法
        pdf = open(ex["pdf"], "rb").read()
        assert pdf.startswith(b"%PDF-1.4") and b"%%EOF" in pdf[-32:]
        # trace/cost JSON 齐备
        assert os.path.exists(os.path.join(os.path.dirname(ex["csv"]),
                                          "trace_replay.json"))
        assert os.path.exists(os.path.join(os.path.dirname(ex["csv"]),
                                          "cost_dashboard.json"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_find_cjk_font_loads_on_windows():
    """本机（Windows）应能找到并加载 CJK 字体；其余环境跳过该断言。"""
    fp = find_cjk_font()
    if fp is None:
        return  # 非 Windows / 无 CJK 字体环境，跳过
    assert os.path.isfile(fp)
    f = _TtfFont(fp)
    # 关键中文字符应映射到非零 GID
    assert f.cmap.get(ord("审"), 0) != 0
    assert f.units_per_em > 0

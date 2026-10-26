"""
audit_suite.py —— 审计三件套产品化封装（独立可演示模块）

把底座已有的三个能力聚合成一份「可演示、可导出」的审计产物：

  1. Trace 回放   —— 复用 TraceStore（trace.py）
  2. 成本看板     —— 复用 CostStore（cost.py）
  3. 审批链导出   —— 复用 ApprovalCenter（approval.py），导出 CSV / SARIF / PDF

设计纪律（与底座一致，改动不可破坏）：
  · 不做任何新存储：只聚合已落库的数据，零新增业务依赖。
  · 旁路语义不破坏：导出失败绝不抛到主链路；Demo 数据走合成轨迹，
    不污染真实业务库（单独落 _demo_*.db）。
  · PDF 零依赖：不依赖 Pillow / Playwright / reportlab / fpdf。优先嵌入系统
    CJK 字体（simhei 等单文件 TrueType）做 Type0 / CIDFontType2（Identity-H），
    无可用字体时降级到 Helvetica（ASCII 安全），保证「任何环境都能产出合法 PDF」。
  · SARIF 自包含：审批链 SARIF 走本模块自己的 writer（scanner/sarif.py 是
    Finding 导向，不混用），但结构兼容 scanner/sarif.py 的 validate_sarif，
    可用同一把尺子做结构自检。
  · 诚实边界：导出产物一律带「数据来源 / 口径说明」，不把量级估算当账单，
    不把审批记录当漏洞判定。

用法（CLI）：
  python audit_suite.py --demo                       # 生成合成数据演示产物
  python audit_suite.py --export-approval <id>        # 导出某审批链（csv/sarif/pdf）
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import struct
import time
import zlib
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from approval import ApprovalCenter, HIGH_RISK_ACTIONS
from cost import CostStore
from trace import TraceStore


# ======================================================================
# 时间格式化（诚实：本机时钟，不做时区换算）
# ======================================================================
def _iso(ts: Any) -> str:
    if ts is None or ts == 0:
        return ""
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OverflowError):
        return str(ts)


# ======================================================================
# 审批链 SARIF writer（自包含，结构兼容 scanner/sarif.validate_sarif）
# ======================================================================
SARIF_VERSION = "2.1.0"
SARIF_SCHEMA = ("https://docs.oasis-open.org/sarif/sarif/v2.1.0/errata01/os/"
                "schemas/sarif-schema-2.1.0.json")

# SARIF level ← 风险（高危动作 → error，其余 → warning）
_LEVEL = {"high": "error", "normal": "warning"}


def approval_to_sarif(chain: Dict[str, Any], tool_name: str = "sectrust-audit",
                      tool_version: str = "1.0.0") -> Dict[str, Any]:
    """把一条审批审计链转成 SARIF 2.1.0 文档（dict）。

    语义：每条 result 对应一次 Human-in-the-Loop 审批决策节点，属「审计证据」
    而非漏洞。高风险动作按 error 标注，便于在 GitHub Code Scanning 面板里被
    优先看到；审计事件链作为 properties.auditTrail 一并携带。
    """
    approval = chain.get("approval") or {}
    events = chain.get("events", [])
    action = approval.get("action", "")
    risk = "high" if action in HIGH_RISK_ACTIONS else "normal"
    rule_id = "approval-decision"

    rules = [{
        "id": rule_id,
        "name": "Human Approval Decision",
        "shortDescription": {"text": "人类审批决策节点（审计链记录）"},
        "fullDescription": {
            "text": "记录一次 Human-in-the-Loop 审批的发起/决策/过期，属审计证据，"
                    "不是漏洞判定。高风险动作（%s）标记 error。"
                    % "、".join(sorted(HIGH_RISK_ACTIONS))},
        "defaultConfiguration": {"level": _LEVEL.get(risk, "warning")},
        "help": {
            "text": "审批链审计导出：每条 result 对应一次审批决策；"
                    "properties.auditTrail 携带完整 Trace 证据。"},
        "properties": {
            "tags": ["audit", "approval", "human-in-the-loop"],
            "kind": "audit-trail",
        },
    }]

    results: List[Dict[str, Any]] = []
    if approval:
        level = _LEVEL.get(risk, "warning")
        msg = ("审批 %s：动作=%s 状态=%s 决策人=%s"
               % (approval.get("approval_id"), action, approval.get("status"),
                  approval.get("decided_by") or "(未决策)"))
        results.append({
            "ruleId": rule_id,
            "ruleIndex": 0,
            "level": level,
            "message": {"text": msg},
            "locations": [{
                "physicalLocation": {
                    "artifactLocation": {
                        "uri": "audit-chain/%s"
                               % (approval.get("task_id") or approval.get("approval_id"))},
                    "region": {"startLine": 1},
                }
            }],
            "properties": {
                "approvalId": approval.get("approval_id"),
                "taskId": approval.get("task_id"),
                "action": action,
                "risk": risk,
                "status": approval.get("status"),
                "channel": approval.get("channel"),
                "decidedBy": approval.get("decided_by"),
                "traceId": chain.get("trace_id"),
                "eventCount": len(events),
                "auditTrail": [
                    {"seq": e.get("seq"), "kind": e.get("kind"),
                     "actor": e.get("actor"), "summary": e.get("summary"),
                     "ts": e.get("ts")}
                    for e in events
                ],
            },
        })

    return {
        "$schema": SARIF_SCHEMA,
        "version": SARIF_VERSION,
        "runs": [{
            "tool": {
                "driver": {
                    "name": tool_name,
                    "semanticVersion": tool_version,
                    "informationUri": "https://github.com/modelcontextprotocol/servers",
                    "rules": rules,
                }
            },
            "results": results,
        }],
    }


# ======================================================================
# 零依赖 PDF writer（优先嵌入 CJK TrueType，否则 Helvetica 降级）
# ======================================================================
def _is_truetype(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(4) == b"\x00\x01\x00\x00"   # 单面 TrueType sfnt
    except OSError:
        return False


def find_cjk_font() -> Optional[str]:
    """查找一个可用的单文件 TrueType CJK 字体（嵌入用）。

    优先 Windows 已知字体；扫描不到则回退 Helvetica（ASCII 安全）。
    不返回 .ttc / .otf（CIDFontType2+FontFile2 只认单面 TrueType）。
    """
    env = os.environ.get("AUDIT_PDF_FONT")
    if env and os.path.isfile(env) and _is_truetype(env):
        return env
    win_fonts = r"C:/Windows/Fonts"
    pref = ["simhei.ttf", "HYZhongHeiTi-197.ttf",  # scanner:allow 字体文件名（标识符熵误报，非凭证）
            "simsunb.ttf", "msyh.ttf"]
    if os.path.isdir(win_fonts):
        for name in pref:
            p = os.path.join(win_fonts, name)
            if os.path.isfile(p) and _is_truetype(p):
                return p
        try:
            for fn in os.listdir(win_fonts):
                if fn.lower().endswith(".ttf"):
                    p = os.path.join(win_fonts, fn)
                    if _is_truetype(p):
                        return p
        except OSError:
            pass
    # 常见 Linux 单文件 CJK TTF（按需增补）
    for p in ("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",  # ttc，跳过
              "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"):
        if os.path.isfile(p) and _is_truetype(p):
            return p
    return None


class _TtfFont:
    """极简 TrueType 读取：只取嵌入 + 排版所需字段（cmap / hmtx / head / hhea）。"""

    __slots__ = ("bytes", "cmap", "widths", "units_per_em",
                 "ascent", "descent", "capheight", "bbox", "ps_name")

    def __init__(self, path: str):
        with open(path, "rb") as fh:
            data = fh.read()
        if data[:4] != b"\x00\x01\x00\x00":
            raise ValueError("非单面 TrueType")
        num = struct.unpack(">H", data[4:6])[0]
        tables: Dict[str, tuple] = {}
        off = 12
        for _ in range(num):
            tag = data[off:off + 4].decode("latin1")
            t_off = struct.unpack(">I", data[off + 8:off + 12])[0]
            t_len = struct.unpack(">I", data[off + 12:off + 16])[0]
            tables[tag] = (t_off, t_len)
            off += 16

        # head
        ho, _ = tables["head"]
        units_per_em = struct.unpack(">H", data[ho + 18:ho + 20])[0]
        x_min, y_min, x_max, y_max = struct.unpack(">hhhh", data[ho + 36:ho + 44])
        # hhea
        heo, _ = tables["hhea"]
        ascender = struct.unpack(">h", data[heo + 4:heo + 6])[0]
        descender = struct.unpack(">h", data[heo + 6:heo + 8])[0]
        num_h_metrics = struct.unpack(">H", data[heo + 34:heo + 36])[0]
        # maxp
        mo, _ = tables["maxp"]
        num_glyphs = struct.unpack(">H", data[mo + 4:mo + 6])[0]
        # hmtx
        hmo, _ = tables["hmtx"]
        widths = [struct.unpack(">H", data[hmo + 4 * i:hmo + 4 * i + 2])[0]
                  for i in range(num_h_metrics)]
        last = widths[-1] if widths else 0
        widths += [last] * (num_glyphs - num_h_metrics)
        # OS/2 sCapHeight（v1+ 在偏移 88）
        cap = ascender
        if "OS/2" in tables:
            o2, _ = tables["OS/2"]
            if len(data) >= o2 + 90:
                cap = struct.unpack(">h", data[o2 + 88:o2 + 90])[0]
        # cmap：取第一个 format 4 子表（覆盖 BMP，含 CJK）
        co, _ = tables["cmap"]
        n_sub = struct.unpack(">H", data[co + 2:co + 4])[0]
        best = None
        for i in range(n_sub):
            base = co + 4 + i * 8
            pid = struct.unpack(">H", data[base:base + 2])[0]
            eid = struct.unpack(">H", data[base + 2:base + 4])[0]
            sub_off = co + struct.unpack(">I", data[base + 4:base + 8])[0]
            fmt = struct.unpack(">H", data[sub_off:sub_off + 2])[0]
            score = 0
            if (pid, eid) == (3, 1):
                score = 3
            elif (pid, eid) == (0, 4):
                score = 2
            elif (pid, eid) == (0, 3):
                score = 1
            if fmt == 4 and (best is None or score > best[0]):
                best = (score, sub_off)
        cmap: Dict[int, int] = {}
        if best is not None:
            cmap = _parse_cmap_f4(data, best[1])

        self.bytes = data
        self.cmap = cmap
        self.widths = widths
        self.units_per_em = units_per_em or 1000
        self.ascent = ascender
        self.descent = descender
        self.capheight = cap
        self.bbox = (x_min, y_min, x_max, y_max)
        self.ps_name = "EmbeddedCJK"


def _parse_cmap_f4(data: bytes, so: int) -> Dict[int, int]:
    """解析 cmap format 4 → {unicode: glyphIndex}。"""
    seg_x2 = struct.unpack(">H", data[so + 6:so + 8])[0]
    seg_count = seg_x2 // 2
    end_base = so + 14
    start_base = end_base + seg_x2 + 2
    delta_base = start_base + seg_x2
    range_base = delta_base + seg_x2
    ends = [struct.unpack(">H", data[end_base + 2 * i:end_base + 2 * i + 2])[0]
            for i in range(seg_count)]
    starts = struct.unpack(">%dH" % seg_count, data[start_base:start_base + 2 * seg_count])
    deltas = struct.unpack(">%dh" % seg_count, data[delta_base:delta_base + 2 * seg_count])
    ranges = struct.unpack(">%dH" % seg_count, data[range_base:range_base + 2 * seg_count])
    out: Dict[int, int] = {}
    for i in range(seg_count):
        start, end, delta, roff = starts[i], ends[i], deltas[i], ranges[i]
        if start == 0xFFFF:
            continue
        for c in range(start, end + 1):
            if roff == 0:
                gid = (c + delta) & 0xFFFF
            else:
                addr = range_base + 2 * (c - start)
                raw = struct.unpack(">H", data[addr:addr + 2])[0]
                gid = (raw + delta) & 0xFFFF if raw != 0 else 0
            out[c] = gid
    return out


class PdfDoc:
    """极简零依赖 PDF 生成器：支持标题/段落/键值，自动分页、自动换行。"""

    A4_W, A4_H, MARGIN = 595.28, 841.89, 50.0

    def __init__(self, font_path: Optional[str] = None):
        self.ttf = None
        try:
            fp = font_path or find_cjk_font()
            if fp:
                self.ttf = _TtfFont(fp)
        except Exception:   # noqa: BLE001 —— 解析失败则降级 Helvetica
            self.ttf = None
        self.pages: List[str] = []
        self._ops: List[str] = []
        self.y = self.A4_H - self.MARGIN

    # ---- 排版基元 ----
    def _char_w(self, ch: str, size: float) -> float:
        if self.ttf:
            gid = self.ttf.cmap.get(ord(ch), 0)
            w = (self.ttf.widths[gid] if gid < len(self.ttf.widths)
                 else (self.ttf.widths[-1] if self.ttf.widths else 500))
            return w / self.ttf.units_per_em * size
        return (0.5 if ord(ch) < 128 else 1.0) * size

    def _wrap(self, text: str, size: float, max_w: float) -> List[str]:
        lines, cur, cur_w, last_space = [], [], 0.0, -1
        for i, ch in enumerate(text):
            cw = self._char_w(ch, size)
            if cur_w + cw > max_w and cur:
                cut = last_space if (last_space >= 0 and last_space < len(cur)) else len(cur)
                lines.append("".join(cur[:cut]))
                cur = cur[cut:] if cut > 0 else []
                cur_w = sum(self._char_w(c, size) for c in cur)
                last_space = -1
            cur.append(ch)
            cur_w += cw
            if ch == " ":
                last_space = len(cur) - 1
        if cur:
            lines.append("".join(cur))
        return lines or [""]

    def _ensure(self, need: float):
        if self.y - need < self.MARGIN:
            self.pages.append("\n".join(self._ops))
            self._ops = []
            self.y = self.A4_H - self.MARGIN

    def _line(self, text: str, size: float, x: float, y: float) -> str:
        if self.ttf:
            hexstr = "<" + "".join("%04X" % self.ttf.cmap.get(ord(c), 0) for c in text) + ">"
            return "BT /F1 %s Tf %s %s Td %s Tj ET" % (size, x, y, hexstr)
        safe = text.encode("latin-1", "replace").decode("latin1")
        safe = safe.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        return "BT /F1 %s Tf %s %s Td (%s) Tj ET" % (size, x, y, safe)

    def add_heading(self, text: str, size: float = 13):
        self._ensure(size + 10)
        self._ops.append(self._line(text, size, self.MARGIN, self.y))
        self.y -= (size + 10)

    def add_paragraph(self, text: str, size: float = 10.5, gap: float = 4.0):
        for ln in self._wrap(text, size, self.A4_W - 2 * self.MARGIN):
            self._ensure(size + gap)
            self._ops.append(self._line(ln, size, self.MARGIN, self.y))
            self.y -= (size + gap)

    def add_kv(self, key: str, value: str, size: float = 10.5):
        self.add_paragraph("%s：%s" % (key, value), size)

    def save(self, path: str) -> str:
        if self._ops:
            self.pages.append("\n".join(self._ops))
            self._ops = []
        pdf = _build_pdf(self.pages, self.ttf)
        tmp = path + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(pdf)
        os.replace(tmp, path)
        return path


def _build_pdf(pages: List[str], ttf: Optional[_TtfFont]) -> bytes:
    """把页面内容流 + 字体装配成合法 PDF（含 xref / trailer）。"""
    catalog_id, pages_id = 1, 2
    next_id = 3
    page_ids, content_ids = [], []
    for _ in range(len(pages)):
        page_ids.append(next_id); next_id += 1
        content_ids.append(next_id); next_id += 1
    if ttf:
        font_id, cidfont_id, desc_id, file_id = next_id, next_id + 1, next_id + 2, next_id + 3
        total = next_id + 3
    else:
        font_id = next_id
        total = next_id
    objs: Dict[int, bytes] = {}

    objs[catalog_id] = ("<< /Type /Catalog /Pages %d 0 R >>" % pages_id).encode("latin1")
    kids = " ".join("%d 0 R" % p for p in page_ids)
    objs[pages_id] = ("<< /Type /Pages /Kids [%s] /Count %d >>"
                      % (kids, len(pages))).encode("latin1")
    for i, content in enumerate(pages):
        pid, cid = page_ids[i], content_ids[i]
        objs[pid] = ("<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %.2f %.2f] "
                     "/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>"
                     % (pages_id, PdfDoc.A4_W, PdfDoc.A4_H, font_id, cid)).encode("latin1")
        cb = content.encode("utf-8")
        objs[cid] = (b"<< /Length %d >>\nstream\n" % len(cb)) + cb + b"\nendstream"

    if ttf:
        objs[font_id] = ("<< /Type /Font /Subtype /Type0 /BaseFont /%s "
                         "/Encoding /Identity-H /DescendantFonts [%d 0 R] >>"
                         % (ttf.ps_name, cidfont_id)).encode("latin1")
        objs[cidfont_id] = ("<< /Type /Font /Subtype /CIDFontType2 /BaseFont /%s "
                            "/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) "
                            "/Supplement 0 >> /FontDescriptor %d 0 R "
                            "/CIDToGIDMap /Identity >>"
                            % (ttf.ps_name, desc_id)).encode("latin1")
        objs[desc_id] = ("<< /Type /FontDescriptor /FontName /%s /Flags 4 "
                         "/FontBBox [%d %d %d %d] /ItalicAngle 0 /Ascent %d "
                         "/Descent %d /CapHeight %d /StemV 80 /FontFile2 %d 0 R >>"
                         % (ttf.ps_name, ttf.bbox[0], ttf.bbox[1], ttf.bbox[2],
                            ttf.bbox[3], ttf.ascent, ttf.descent, ttf.capheight,
                            file_id)).encode("latin1")
        fb = ttf.bytes
        fb_comp = zlib.compress(fb, 9)   # FlateDecode：9.7MB → ~3.5MB，零依赖安全压缩
        objs[file_id] = (b"<< /Length %d /Filter /FlateDecode >>\nstream\n"
                         % len(fb_comp)) + fb_comp + b"\nendstream"
    else:
        objs[font_id] = (b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
                         b"/Encoding /WinAnsiEncoding >>")

    out = bytearray()
    out += b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
    offsets: Dict[int, int] = {}
    for oid in range(1, total + 1):
        offsets[oid] = len(out)
        out += ("%d 0 obj\n" % oid).encode("latin1")
        out += objs[oid]
        out += b"\nendobj\n"
    xref_off = len(out)
    out += ("xref\n0 %d\n" % (total + 1)).encode("latin1")
    out += b"0000000000 65535 f \n"
    for oid in range(1, total + 1):
        out += ("%010d 00000 n \n" % offsets[oid]).encode("latin1")
    out += ("trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (total + 1, catalog_id, xref_off)).encode("latin1")
    return bytes(out)


# ======================================================================
# 审计三件套聚合封装
# ======================================================================
class AuditSuite:
    """把 Trace / Cost / Approval 三个底座能力聚合成可演示、可导出的审计产物。

    trace / cost / approval 可传存储实例，也可传 db 路径（自动装配）。
    传实例时本类不持有其生命周期（调用方负责 close）；传路径时本类建连并负责关闭。
    """

    def __init__(self, trace: Any = None, cost: Any = None, approval: Any = None):
        # 传实例 → 不持有生命周期；传路径/None → 本类建连并负责 close
        self._owns: Dict[str, bool] = {}
        self.trace, self._owns["trace"] = self._resolve(TraceStore, trace)
        self.cost, self._owns["cost"] = self._resolve(CostStore, cost)
        # 审批中心与轨迹库统一：构造时把同一个 trace 存储注入，
        # 使 audit() 能从统一轨迹取证据（兼容审批库/轨迹库分离部署）。
        if approval is None or isinstance(approval, str):
            self.approval = (ApprovalCenter(approval, tracer=self.trace)
                             if approval else ApprovalCenter(tracer=self.trace))
            self._owns["approval"] = True
        else:
            self.approval = approval
            self._owns["approval"] = False

    @staticmethod
    def _resolve(cls, arg):
        if arg is None or isinstance(arg, str):
            store = cls(arg) if arg else cls()
            return store, True
        return arg, False

    def close(self):
        for key in ("trace", "cost", "approval"):
            store = getattr(self, key, None)
            if store is not None and self._owns.get(key):
                try:
                    store.close()
                except Exception:   # noqa: BLE001
                    pass

    # ---- 聚合读取 ----
    def trace_replay(self, trace_id: str) -> Dict[str, Any]:
        return {"trace_id": trace_id, "meta": self.trace.get(trace_id),
                "events": self.trace.events(trace_id)}

    def cost_dashboard(self, days: int = 30, top: int = 5) -> Dict[str, Any]:
        return self.cost.dashboard(days=days, top=top)

    def approval_audit(self, approval_id: str) -> Dict[str, Any]:
        return self.approval.audit(approval_id)

    # ---- 审批链导出 ----
    def export_approval(self, approval_id: str, out_dir: str,
                        formats: List[str] = None) -> Dict[str, Any]:
        formats = formats or ["csv", "sarif", "pdf"]
        os.makedirs(out_dir, exist_ok=True)
        chain = self.approval.audit(approval_id)
        paths: Dict[str, Any] = {"found": chain.get("approval") is not None,
                                 "approval_id": approval_id}
        if "csv" in formats:
            paths["csv"] = self._export_csv(chain, out_dir, approval_id)
        if "sarif" in formats:
            paths["sarif"] = self._export_sarif(chain, out_dir, approval_id)
        if "pdf" in formats:
            paths["pdf"] = self._export_pdf(chain, out_dir, approval_id)
        return paths

    def _export_csv(self, chain, out_dir, aid) -> str:
        approval = chain.get("approval") or {}
        risk = "high" if approval.get("action") in HIGH_RISK_ACTIONS else "normal"
        created, decided = approval.get("created_at"), approval.get("decided_at")
        latency = round(decided - created, 1) if created and decided else ""
        path = os.path.join(out_dir, "approval_%s.csv" % aid)
        with open(path, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["approval_id", "record_type", "task_id", "action", "risk",
                        "status", "channel", "decided_by", "created_at",
                        "decided_at", "latency_s", "trace_id", "seq", "kind",
                        "actor", "summary", "ts", "detail"])
            w.writerow([aid, "approval", approval.get("task_id"),
                        approval.get("action"), risk, approval.get("status"),
                        approval.get("channel"), approval.get("decided_by"),
                        _iso(created), _iso(decided), latency,
                        chain.get("trace_id"), "", "", "", "",
                        _iso(created), approval.get("detail") or ""])
            for e in chain.get("events", []):
                w.writerow([aid, "event", approval.get("task_id"), "", "", "", "",
                            "", _iso(created), _iso(decided), latency,
                            chain.get("trace_id"), e.get("seq"), e.get("kind"),
                            e.get("actor"), e.get("summary"), _iso(e.get("ts")),
                            json.dumps(e.get("detail"), ensure_ascii=False)
                            if e.get("detail") else ""])
        return path

    def _export_sarif(self, chain, out_dir, aid) -> str:
        doc = approval_to_sarif(chain)
        path = os.path.join(out_dir, "approval_%s.sarif" % aid)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(doc, ensure_ascii=False, indent=2))
        os.replace(tmp, path)
        return path

    def _export_pdf(self, chain, out_dir, aid) -> str:
        approval = chain.get("approval") or {}
        risk = "high" if approval.get("action") in HIGH_RISK_ACTIONS else "normal"
        created, decided = approval.get("created_at"), approval.get("decided_at")
        latency = round(decided - created, 1) if created and decided else ""
        events = chain.get("events", [])
        doc = PdfDoc()
        doc.add_heading("审批链审计报告 / Approval Chain Audit Report", 15)
        doc.add_paragraph("本报告由「审计三件套」自动导出，记录一次 Human-in-the-Loop "
                          "审批的完整证据链（含 Trace 轨迹事件）。", 10)
        doc.add_heading("一、审批概要", 12)
        for k, v in [("审批单号", approval.get("approval_id")),
                     ("关联任务", approval.get("task_id")),
                     ("动作", approval.get("action")),
                     ("风险等级", risk),
                     ("状态", approval.get("status")),
                     ("渠道", approval.get("channel")),
                     ("决策人", approval.get("decided_by")),
                     ("发起时间", _iso(created)),
                     ("决策时间", _iso(decided)),
                     ("决策时延(秒)", latency),
                     ("轨迹编号", chain.get("trace_id"))]:
            doc.add_kv(k, str(v) if v is not None else "-")
        doc.add_heading("二、审计事件链（Trace 证据）", 12)
        if events:
            for e in events:
                doc.add_paragraph("#%s [%s] %s  actor=%s  %s"
                                  % (e.get("seq"), e.get("kind"), e.get("summary"),
                                     e.get("actor") or "-", _iso(e.get("ts"))), 9.5)
        else:
            doc.add_paragraph("（无轨迹上下文：该审批未挂接 Trace，证据缺口已如实标注）", 9.5)
        doc.add_heading("三、诚实边界声明", 12)
        doc.add_paragraph("· 本报告仅记录审批决策与轨迹证据，不构成漏洞或风险判定。", 9)
        doc.add_paragraph("· 风险等级依据动作是否属于高危集（%s）自动标注，属策略口径而非扫描结论。"
                          % "、".join(sorted(HIGH_RISK_ACTIONS)), 9)
        doc.add_paragraph("· 时间戳来自本机时钟，跨时区展示未经换算。", 9)
        path = os.path.join(out_dir, "approval_%s.pdf" % aid)
        return doc.save(path)


# ======================================================================
# Demo：合成数据 → 生成可演示产物（HTML + 导出样例）
# ======================================================================
def _render_demo_html(aid: str, paths: Dict[str, Any], trace_data: Dict[str, Any],
                      cost_data: Dict[str, Any], export_dir: str, out_dir: str) -> str:
    def rel(p: str) -> str:
        return os.path.relpath(p, out_dir).replace("\\", "/") if p else ""

    ev_rows = "".join(
        "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
        % (e.get("seq"), e.get("kind"), (e.get("actor") or "-"),
           (e.get("summary") or ""), _iso(e.get("ts")))
        for e in (trace_data.get("events") or []))
    meta = trace_data.get("meta") or {}
    tot = cost_data.get("totals", {})
    cost_rows = (
        "<tr><td>任务数</td><td>%s</td></tr>"
        "<tr><td>模型调用</td><td>%s</td></tr>"
        "<tr><td>Token 总量</td><td>%s</td></tr>"
        "<tr><td>工具调用</td><td>%s</td></tr>"
        "<tr><td>重试</td><td>%s</td></tr>"
        "<tr><td>成本估算(USD, 量级)</td><td>%s</td></tr>"
        "<tr><td>无法计价调用</td><td>%s</td></tr>"
        % (tot.get("tasks"), tot.get("model_calls"), tot.get("tokens"),
           tot.get("tool_calls"), tot.get("retries"), tot.get("cost_estimate"),
           tot.get("unpriced_calls")))
    csv_l = rel(paths.get("csv", ""))
    sarif_l = rel(paths.get("sarif", ""))
    pdf_l = rel(paths.get("pdf", ""))
    trace_l = rel(os.path.join(export_dir, "trace_replay.json"))
    cost_l = rel(os.path.join(export_dir, "cost_dashboard.json"))
    return """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>审计三件套 · 产品化演示</title>
<style>
  :root{--bg:#f6f8fb;--card:#fff;--ink:#1f2933;--muted:#66727f;--brand:#2b6cb0;--line:#e3e8ef;}
  *{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--ink);
    font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;line-height:1.6}
  header{background:linear-gradient(120deg,#2b6cb0,#3b8ed0);color:#fff;padding:30px 24px}
  header h1{margin:0 0 6px;font-size:24px} header p{margin:0;opacity:.92;font-size:14px}
  main{max-width:980px;margin:0 auto;padding:22px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:12px;
    padding:18px 20px;margin-bottom:18px;box-shadow:0 1px 3px rgba(16,24,40,.04)}
  .card h2{margin:0 0 12px;font-size:18px;color:var(--brand)}
  .tag{display:inline-block;background:#eef4fb;color:var(--brand);border-radius:6px;
    padding:2px 8px;font-size:12px;margin-left:6px}
  table{width:100%%;border-collapse:collapse;font-size:13px}
  th,td{border:1px solid var(--line);padding:7px 9px;text-align:left}
  th{background:#f1f5f9;font-weight:600}
  .downloads a{display:inline-block;margin:6px 8px 6px 0;padding:8px 14px;
    background:var(--brand);color:#fff;border-radius:8px;text-decoration:none;font-size:13px}
  .note{color:var(--muted);font-size:12.5px;margin-top:10px}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
  .kpi{background:#f1f5f9;border-radius:8px;padding:10px}
  .kpi b{display:block;font-size:20px;color:var(--brand)} .kpi span{font-size:12px;color:var(--muted)}
</style></head>
<body>
<header><h1>审计三件套 · 产品化演示</h1>
<p>Trace 回放 · 成本看板 · 审批链导出（CSV / SARIF / PDF）｜ 独立可演示模块</p></header>
<main>

<div class="card"><h2>① Trace 回放 <span class="tag">trace.py</span></h2>
<p class="note">轨迹：%(title)s ｜ 类型：%(kind)s ｜ 状态：%(status)s ｜ 事件数：%(ecount)s</p>
<table><thead><tr><th>seq</th><th>kind</th><th>actor</th><th>summary</th><th>时间</th></tr></thead>
<tbody>%(ev_rows)s</tbody></table>
<p class="downloads"><a href="%(trace_l)s" download>下载 trace_replay.json</a></p>
</div>

<div class="card"><h2>② 成本看板 <span class="tag">cost.py</span></h2>
<div class="grid">
  <div class="kpi"><b>%(tasks)s</b><span>任务数</span></div>
  <div class="kpi"><b>%(mc)s</b><span>模型调用</span></div>
  <div class="kpi"><b>%(tokens)s</b><span>Token 总量</span></div>
  <div class="kpi"><b>%(tc)s</b><span>工具调用</span></div>
  <div class="kpi"><b>%(cost)s</b><span>成本估算(USD)</span></div>
</div>
<table style="margin-top:12px">%(cost_rows)s</table>
<p class="note">%(price_note)s</p>
<p class="downloads"><a href="%(cost_l)s" download>下载 cost_dashboard.json</a></p>
</div>

<div class="card"><h2>③ 审批链导出 <span class="tag">approval.py</span></h2>
<p class="note">演示审批单：<b>%(aid)s</b>（已挂接 Trace，含 %(evcount)s 条审计事件）</p>
<p class="downloads">
  <a href="%(csv_l)s" download>CSV</a>
  <a href="%(sarif_l)s" download>SARIF</a>
  <a href="%(pdf_l)s" download>PDF</a>
</p>
<p class="note">三种格式来自同一份审批审计链（approval.audit）：CSV 供表格复核，
SARIF 可进 GitHub Code Scanning / VS Code，PDF 为可打印正式件（零依赖生成，内嵌 CJK 字体）。</p>
</div>

<div class="card"><h2>诚实边界（标准范式）</h2>
<p>以上数据来自单主机 + 同机多进程的合成演示环境，<b>不可外推到分散物理机</b>；
成本估算为公开挂牌价静态快照的量级值，<b>非账单</b>；审批记录是审计证据，<b>非漏洞判定</b>。
完整范式见 <code>docs/honest_boundary_paradigm.md</code>。</p>
</div>

</main></body></html>
""" % {
        "title": meta.get("title", "-"), "kind": meta.get("kind", "-"),
        "status": meta.get("status", "-"), "ecount": meta.get("event_count", 0),
        "ev_rows": ev_rows, "trace_l": trace_l,
        "tasks": tot.get("tasks"), "mc": tot.get("model_calls"),
        "tokens": tot.get("tokens"), "tc": tot.get("tool_calls"),
        "cost": tot.get("cost_estimate"), "cost_rows": cost_rows,
        "price_note": cost_data.get("price_note", ""), "cost_l": cost_l,
        "aid": aid, "evcount": len(trace_data.get("events") or []),
        "csv_l": csv_l, "sarif_l": sarif_l, "pdf_l": pdf_l,
    }


def demo(out_dir: str = "reports/audit_suite_demo") -> Dict[str, Any]:
    """生成合成数据演示产物：HTML + 审批链导出样例（csv/sarif/pdf）+ 两份 JSON。

    全程使用独立 _demo_*.db，绝不污染真实业务库。
    """
    os.makedirs(out_dir, exist_ok=True)
    export_dir = os.path.join(out_dir, "exports")
    os.makedirs(export_dir, exist_ok=True)
    trace_db = os.path.join(out_dir, "_demo_trace.db")
    cost_db = os.path.join(out_dir, "_demo_cost.db")
    appr_db = os.path.join(out_dir, "_demo_approval.db")
    for p in (trace_db, cost_db, appr_db):
        if os.path.exists(p):
            os.remove(p)

    ts = TraceStore(trace_db)
    cs = CostStore(cost_db)
    ac = ApprovalCenter(appr_db, tracer=ts)   # 审批轨迹与流水线轨迹共用 ts
    try:
        # 合成一条「双十一素材发布」流水线轨迹
        tid = ts.start_trace(title="双十一大促素材生成流水线", kind="pipeline",
                             started_by="planner")
        ts.record(tid, "plan_built", summary="拆解 5 道工序", detail={"steps": 5})
        t1 = "task_demo_01"
        ts.record(tid, "task_registered", task_id=t1, summary="登记素材生成任务",
                  detail={"depends_on": []})
        ts.record(tid, "task_claimed", task_id=t1, actor="designer-bot",
                  summary="designer-bot 认领")
        aid = ac.request("publish", "将生成素材发布到生产 CDN", task_id=t1,
                         channel="web", user_id="ops-1", trace_id=tid)
        ac.decide(aid, "approve", by="human-ops")
        ts.record(tid, "tool_call", task_id=t1, actor="designer-bot",
                  summary="调用 CDN 上传工具", detail={"tool": "cdn_upload"})
        ts.record(tid, "task_completed", task_id=t1, actor="designer-bot",
                  summary="素材发布完成")
        ts.finish(tid, status="completed")

        # 合成成本
        cs.record_model_call(task_id=t1, trace_id=tid, model="gpt-4o",
                             input_tokens=12000, output_tokens=3000,
                             actor="designer-bot")
        cs.record_tool_call(task_id=t1, trace_id=tid, tool="cdn_upload",
                            actor="designer-bot")
        cs.finalize_task(t1, trace_id=tid, role="designer",
                         template="promo-material", tier="pro", outcome="completed")

        suite = AuditSuite(trace=ts, cost=cs, approval=ac)
        paths = suite.export_approval(aid, out_dir=export_dir,
                                      formats=["csv", "sarif", "pdf"])
        trace_data = suite.trace_replay(tid)
        cost_data = suite.cost_dashboard(days=30)
        with open(os.path.join(export_dir, "trace_replay.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(trace_data, fh, ensure_ascii=False, indent=2, default=str)
        with open(os.path.join(export_dir, "cost_dashboard.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(cost_data, fh, ensure_ascii=False, indent=2, default=str)

        html = _render_demo_html(aid, paths, trace_data, cost_data, export_dir, out_dir)
        html_path = os.path.join(out_dir, "audit_suite_demo.html")
        with open(html_path, "w", encoding="utf-8") as fh:
            fh.write(html)

        return {"html": html_path, "exports": paths, "demo_dir": out_dir}
    finally:
        # 任何异常下都关闭连接，避免 -W error::ResourceWarning 下泄漏
        ts.close(); cs.close(); ac.close()


# ======================================================================
# CLI
# ======================================================================
def main(argv=None):
    p = argparse.ArgumentParser(description="审计三件套产品化模块（独立可演示）")
    p.add_argument("--demo", action="store_true", help="生成合成数据演示产物")
    p.add_argument("--out-dir", default="reports/audit_suite_demo")
    p.add_argument("--export-approval", metavar="APPROVAL_ID",
                   help="导出某审批链（csv/sarif/pdf）")
    p.add_argument("--approval-db", default=None)
    p.add_argument("--trace-db", default=None)
    p.add_argument("--cost-db", default=None)
    p.add_argument("--formats", default="csv,sarif,pdf")
    p.add_argument("--export-dir", default=None)
    a = p.parse_args(argv)

    if a.demo:
        r = demo(out_dir=a.out_dir)
        print(json.dumps(r, ensure_ascii=False, indent=2))
    elif a.export_approval:
        suite = AuditSuite(trace=a.trace_db, cost=a.cost_db, approval=a.approval_db)
        try:
            paths = suite.export_approval(
                a.export_approval,
                out_dir=a.export_dir or "reports/audit_suite_exports",
                formats=[f.strip() for f in a.formats.split(",") if f.strip()])
            print(json.dumps(paths, ensure_ascii=False, indent=2))
        finally:
            suite.close()
    else:
        p.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""
execution/writer.py —— 让 AI 同事"直接把文件写进仓库"（此前缺失的落盘环节）。

背景（补齐的缺口）：
    software_dev 模板里「前端实现 / 后端实现」工序会产出代码，但那只是**模型
    返回的一段 markdown 文本**——代码块躺在 answer 里，没人把它变成磁盘上的
    文件。此前唯一能落盘的路子是 op="shell" 让模型自己拼 `echo`/`cat >` 命令，
    既不可控（引号/换行/编码随时炸）也不安全（无法预检要写去哪）。

本模块提供 op="write_files"：
    · 输入可以是**显式清单** {"path":..., "content":...}，也可以是**模型产出
      原文**（由 extract_code_blocks 逐行解析出 fenced 代码块 + 推断文件名）；
    · 落盘全程走 BotWorkspace._resolve() 做路径校验，**拒绝越出工作区**；
    · 追加防护：拒绝覆盖仓库基础设施（.git/、data/ 数据库、密钥/证书文件）；
    · 原子写（同目录临时文件 + os.replace），写一半断电不会留下半个文件；
    · **落盘后立刻跑密钥扫描**（复用 scanner 包）：AI 同事写进去的代码里若
      混了真实凭证，当场报出来——这是本产品"协作审计台"的本分。

诚实边界（务必）：
    · 本模块**只写文件、不执行命令、不做 git 操作**（commit/push 仍归人工或
      上层流程，AI 同事无权代提交）；
    · 扫描是"检测"不是"修复"：命中只报告，不擅自删改用户代码；
    · 越界/敏感路径一律拒写并给出原因，**绝不静默跳过也绝不强行改写**；
    · 文件名由语言推断时会标注 inferred=true，不假装模型真的指定了名字。
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from typing import Optional

# --------------------------------------------------------------------------
# 代码块解析（逐行状态机，O(n) 且无回溯 —— 遵守 ReDoS 纪律：不用惰性量词
# 跨行匹配，逐行扫 + 开关状态，长文本也不会指数回溯）
# --------------------------------------------------------------------------

# 语言别名 → 扩展名（推断文件名用；没映射的语言退回 .txt）
_LANG_EXT = {
    "python": "py", "py": "py", "python3": "py",
    "javascript": "js", "js": "js", "node": "js",
    "typescript": "ts", "ts": "ts", "tsx": "tsx",
    "jsx": "jsx", "react": "jsx",
    "html": "html", "htm": "html", "css": "css", "scss": "scss",
    "json": "json", "jsonc": "json",
    "yaml": "yaml", "yml": "yml", "toml": "toml", "ini": "ini",
    "bash": "sh", "sh": "sh", "shell": "sh", "zsh": "sh",
    "sql": "sql", "go": "go", "golang": "go",
    "rust": "rs", "rs": "rs",
    "java": "java", "kotlin": "kt", "swift": "swift",
    "c": "c", "cpp": "cpp", "c++": "cpp", "csharp": "cs", "cs": "cs",
    "php": "php", "ruby": "rb", "rb": "rb",
    "markdown": "md", "md": "md", "mdx": "mdx",
    "dockerfile": "Dockerfile", "docker": "Dockerfile",
    "makefile": "Makefile", "make": "Makefile",
    "xml": "xml", "csv": "csv",
}

# 从 info string 或首行注释里抓显式文件名：
#   ```python title="src/app.py"   /   # file: src/app.py   /  // path=app.js
_FILE_HINT_RE = re.compile(
    r'(?:^|[\s\(\[<])(?:title|file|path|filename|name)\s*[=:]\s*'
    r'["\'`]?([A-Za-z0-9][A-Za-z0-9._/\-]{0,180}\.[A-Za-z0-9]{1,8})["\'`]?'
)

# 另一种常见写法：文件名单独成行（如 "src/app.py:" 或 "`src/app.py`:"）
_BARE_PATH_RE = re.compile(
    r'^[`"\']?([A-Za-z0-9][A-Za-z0-9._/\-]{0,180}\.[A-Za-z0-9]{1,8})[`"\']?\s*[:：]?\s*$'
)


def _fence_open(line: str):
    """判断一行是否为栅栏开头。返回 (char, lang, info) 或 None。

    逐行判断，正则只作用于单行（无跨行量词），O(n) 无回溯风险。
    """
    s = line.strip()
    if not s:
        return None
    ch = s[0]
    if ch not in "`~":
        return None
    k = 0
    while k < len(s) and s[k] == ch:
        k += 1
    if k < 3:
        return None
    rest = s[k:].strip()
    # rest 形如：python title="a.py"  /  python  /  （空）
    m = re.match(r"^([A-Za-z0-9_+#.\-]*)(?:\s+(.*))?$", rest)
    lang = (m.group(1) or "") if m else ""
    info = (m.group(2) or "") if m else ""
    return ch, lang.lower(), info


def _fence_close(line: str, ch: str) -> bool:
    """是否为配对的结束栅栏（同字符、长度 ≥ 开头、整行无别的字符）。"""
    s = line.strip()
    if not s or s[0] != ch:
        return False
    return len(s) >= 3 and set(s) == {ch}


def _normalize_rel(path: str) -> Optional[str]:
    """把模型给的文件名规整为安全相对路径；不合法返回 None。

    拒绝：绝对路径、含 ".." 段、空、以 "/" 开头、盘符（C:）、UNC（\\\\）。
    """
    if not isinstance(path, str):
        return None
    p = path.strip().strip("`\"'").replace("\\", "/")
    p = re.sub(r"\s+", "", p)          # 防 "src/ app.py" 这种被空格污染
    if not p or p.startswith("/") or p.startswith("../"):
        return None
    if re.match(r"^[A-Za-z]:", p):     # 盘符
        return None
    parts = [seg for seg in p.split("/") if seg not in ("", ".")]
    if not parts or ".." in parts:
        return None
    return "/".join(parts)


def extract_code_blocks(text: str) -> list:
    """从模型产出的 markdown 原文里提取代码块，尽量还原文件名。

    返回 [{"path","content","lang","inferred"}]；无法定位文件名的代码块**不会**
    被丢弃，而是用语言推断一个名字并标 inferred=true（不假装模型指定了）。

    文件名来源优先级：
      1) info string（```python title="a.py"）；
      2) 代码块首行注释（# file: a.py / // file: a.py）；
      3) 代码块前一行的说明文字里单独出现的路径；
      4) 语言推断（main.py / main_2.py ...）。
    """
    if not isinstance(text, str) or not text.strip():
        return []
    lines = text.splitlines()
    out = []
    used = set()
    counters = {}
    i, n = 0, len(lines)
    while i < n:
        opened = _fence_open(lines[i])
        if not opened:
            i += 1
            continue
        ch, lang, info = opened
        body = []
        i += 1
        while i < n:
            if _fence_close(lines[i], ch):
                i += 1
                break
            body.append(lines[i])
            i += 1
        content = "\n".join(body)

        # 1) info string
        rel = None
        m = _FILE_HINT_RE.search(info or "")
        if m:
            rel = _normalize_rel(m.group(1))

        # 2) 首行注释
        if not rel and body:
            first = body[0]
            m2 = _FILE_HINT_RE.search(first)
            if m2:
                rel = _normalize_rel(m2.group(1))
                if rel:
                    # 首行若只是 "# file: x.py" 这种标注，不应算作代码正文
                    if re.match(r"^\s*(#|//|/\*|<!--)\s*(title|file|path|filename|name)\s*[=:]",
                                first, re.I):
                        content = "\n".join(body[1:]).lstrip("\n")
            else:
                # 3) 代码块前一行（"src/app.py:" 这种）
                prev = lines[i - len(body) - 2] if (i - len(body) - 2) >= 0 else ""
                m3 = _BARE_PATH_RE.match((prev or "").strip())
                if m3:
                    rel = _normalize_rel(m3.group(1))

        # 4) 语言推断
        inferred = False
        if not rel:
            ext = _LANG_EXT.get(lang, "txt")
            base = "main"
            counters[base + "." + ext] = counters.get(base + "." + ext, 0) + 1
            idx = counters[base + "." + ext]
            rel = "%s.%s" % (base, ext) if idx == 1 else "%s_%d.%s" % (base, idx, ext)
            inferred = True
        # 同名冲突（模型给了重复文件名）→ 加序号，不互相覆盖
        if rel in used:
            stem, dot, ext = rel.rpartition(".")
            k = 2
            while "%s_%d.%s" % (stem, k, ext) in used:
                k += 1
            rel = "%s_%d.%s" % (stem, k, ext)
        used.add(rel)
        out.append({"path": rel, "content": content,
                    "lang": lang, "inferred": inferred})
    return out


# --------------------------------------------------------------------------
# 路径安全：敏感目标拒写（在 _resolve 的越界防护之上再加一层"别动仓库根基"）
# --------------------------------------------------------------------------

_SENSITIVE_DIRS = (".git", "data", ".workbuddy", "node_modules", "__pycache__",
                   "dist_build", "release_meta", ".venv", "venv")
_SENSITIVE_EXTS = (".db", ".sqlite", ".sqlite3", ".pem", ".key", ".p12",
                   ".pfx", ".keystore", ".jks")
_SENSITIVE_NAME_RE = re.compile(
    r"^(?:\.env|id_rsa|id_dsa|id_ecdsa|id_ed25519|\.npmrc|\.pypirc|"
    r"credentials|secrets?)(?:\..*)?$", re.I
)


def is_sensitive(rel: str) -> Optional[str]:
    """相对路径是否指向"不该由 AI 同事动"的目标。返回拒绝原因或 None。"""
    if not rel:
        return "空路径"
    parts = rel.replace("\\", "/").split("/")
    for seg in parts:
        if seg.lower() in {d.lower() for d in _SENSITIVE_DIRS}:
            return "拒绝写入受保护目录：%s/" % seg
    leaf = parts[-1]
    if _SENSITIVE_NAME_RE.match(leaf):
        return "拒绝写入敏感文件：%s" % leaf
    if leaf.lower().endswith(_SENSITIVE_EXTS):
        return "拒绝写入敏感后缀文件：%s" % leaf
    return None


# --------------------------------------------------------------------------
# 落盘
# --------------------------------------------------------------------------


class FileWriteError(ValueError):
    """落盘前置校验失败（清单为空/格式不对/整批越界）。"""


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_files(files) -> list:
    """把 payload 里的 files 归一化校验。非法项不静默丢弃，记 blocked 原因。"""
    normalized, blocked = [], []
    if not isinstance(files, list) or not files:
        raise FileWriteError("write_files 需要非空的 files 列表")
    for i, item in enumerate(files):
        if not isinstance(item, dict):
            blocked.append({"index": i, "path": "", "reason": "条目不是对象"})
            continue
        raw_path = item.get("path") or item.get("name") or ""
        content = item.get("content")
        if content is None:
            content = item.get("code") or ""      # 兼容 {"code": "..."}
        rel = _normalize_rel(raw_path)
        if not rel:
            blocked.append({"index": i, "path": str(raw_path)[:120],
                            "reason": "路径不合法（须为不含 .. 的相对路径）"})
            continue
        if not isinstance(content, str):
            blocked.append({"index": i, "path": rel,
                            "reason": "content 必须是字符串"})
            continue
        normalized.append({"path": rel, "content": content})
    return normalized, blocked


def write_files(workspace, files, blocked: list = None, root: str = None,
                allow_outside: bool = False,
                overwrite: bool = False, scan: bool = True,
                max_bytes: int = 1_000_000) -> dict:
    """把文件列表落盘到工作区（默认 work/），可选落盘后密钥扫描。

    workspace：execution.workspace.BotWorkspace 实例（路径校验走它的 _resolve）。
    files：已归一化的清单 [{"path","content"}]（由 plan_from_payload 产出）。
    blocked：上游解析阶段已判定的拒写项，会合并进结果一起报（不丢信息）。

    返回结构化 result（供 complete(result=...) 与审计/UI 消费）：
        {"op":"write_files","root":...,"written":[{path,bytes,sha256,overwrote}],
         "blocked":[{path,reason}],"scan":{...}}

    纪律：越界/敏感 → 拒写并给原因；扫描出错 → 记进 scan["errors"]，
    绝不静默说"干净"。
    """
    if blocked is None:
        normalized, blocked = normalize_files(files)
    else:
        normalized, extra = normalize_files(files)
        blocked = list(blocked) + extra

    # ---- 落盘根目录安全判定 ----
    base = os.path.abspath(root or workspace.work_dir)
    ws_dir = os.path.abspath(workspace.dir)
    if base != ws_dir and not base.startswith(ws_dir + os.sep):
        if not allow_outside:
            # 整批拒写：不给"部分成功"的假象
            return {"op": "write_files", "root": base, "written": [],
                    "blocked": [{"path": f["path"], "reason":
                                 "落盘根目录在工作区之外（需显式 allow_outside）"}
                                for f in normalized] + blocked,
                    "error": "拒绝写入工作区之外的路径"}
        os.makedirs(base, exist_ok=True)

    written = []
    for item in normalized:
        rel = item["path"]
        why = is_sensitive(rel)
        if why:
            blocked.append({"path": rel, "reason": why})
            continue
        try:
            # 先按"工作区为根"解析，确认无穿越；再换算到实际 base
            safe_rel = workspace._resolve("work", rel)
            work_root = os.path.abspath(workspace.work_dir)
            tail = os.path.relpath(safe_rel, work_root)
        except ValueError as e:
            blocked.append({"path": rel, "reason": str(e)})
            continue
        target = os.path.join(base, tail)
        target = os.path.abspath(target)
        if target != base and not target.startswith(base + os.sep):
            blocked.append({"path": rel, "reason": "拒绝越界路径"})
            continue
        data = item["content"]
        if len(data.encode("utf-8")) > max_bytes:
            blocked.append({"path": rel,
                            "reason": "文件超过大小上限（%d 字节）" % max_bytes})
            continue
        existed = os.path.isfile(target)
        if existed and not overwrite:
            blocked.append({"path": rel, "reason": "目标已存在（未声明 overwrite）"})
            continue
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            # 原子写：同目录临时文件 + os.replace
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(target),
                                       prefix=".write-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                    f.write(data)
                os.replace(tmp, target)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError as e:
            blocked.append({"path": rel, "reason": "写入失败：%s" % e})
            continue
        written.append({"path": tail.replace("\\", "/"),
                        "abs_path": target,
                        "bytes": len(data.encode("utf-8")),
                        "sha256": _sha256(data),
                        "overwrote": existed})
    result = {"op": "write_files", "root": base,
              "written": written, "blocked": blocked}

    # ---- 落盘后密钥扫描：AI 同事写进去的凭证必须当场现形 ----
    if scan and written:
        result["scan"] = _scan_written(written)
    # 人类可读摘要：UI / 审计 / 下游 quality 规则都要读它，别让结果只剩一堆哈希
    result["output"] = summary_text(result)
    return result


def summary_text(result: dict) -> str:
    """把落盘结果讲成人话（含拒写原因与扫描结论）。"""
    written = result.get("written") or []
    lines = ["已落盘 %d 个文件（根目录：%s）" % (len(written), result.get("root"))]
    for w in written:
        lines.append("  · %s（%d 字节，sha256=%s…%s）" % (
            w.get("path"), w.get("bytes", 0), (w.get("sha256") or "")[:12],
            "，已覆盖原文件" if w.get("overwrote") else ""))
    blocked = result.get("blocked") or []
    if blocked:
        lines.append("拒写 %d 项：" % len(blocked))
        for b in blocked:
            lines.append("  · %s：%s" % (b.get("path") or "(未知路径)",
                                         b.get("reason") or ""))
    scan = result.get("scan")
    if scan:
        if scan.get("error"):
            lines.append("⚠ 密钥扫描未完成：%s（不敢称『干净』）" % scan["error"])
        else:
            summary = scan.get("summary") or {}
            lines.append("密钥扫描：%d 处命中，闸门%s" % (
                summary.get("total", 0),
                "触发（高危）" if scan.get("should_fail") else "通过"))
            if scan.get("errors"):
                lines.append("  其中 %d 个文件扫描出错（未计入结论）"
                             % len(scan["errors"]))
    return "\n".join(lines)


def _scan_written(written: list) -> dict:
    """对刚落盘的文件逐个跑 scanner（结构化规则 + 熵兜底）。"""
    try:
        from scanner import scan_file, summarize, gate
    except ImportError:
        return {"scanned": 0, "error": "扫描引擎不可用（未扫描，不敢称干净）"}
    findings, errors = [], []
    for w in written:
        try:
            findings.extend(scan_file(w["abs_path"]))
        except Exception as e:      # noqa: BLE001 —— 单个文件扫不动要记账，不静默
            errors.append({"path": w["path"], "error": str(e)})
    try:
        summary = summarize(findings)
        verdict = gate(findings, fail_on="high")
    except Exception as e:          # noqa: BLE001
        return {"scanned": len(written), "errors": errors,
                "error": "扫描结果汇总失败：%s" % e}
    out = {"scanned": len(written), "summary": summary,
           "should_fail": verdict.get("should_fail"),
           "fail_reason": verdict.get("reason") or "",
           "findings": [f.to_dict() for f in findings[:50]]}
    if errors:
        out["errors"] = errors
    return out


def plan_from_payload(payload: dict) -> tuple:
    """从 payload 解析出待写文件清单（内联 files 或模型原文 extract）。"""
    files = payload.get("files")
    if files:
        return normalize_files(files)
    text = payload.get("extract")
    if text:
        blocks = extract_code_blocks(text)
        if not blocks:
            return [], [{"path": "", "reason": "原文中未找到 fenced 代码块"}]
        return blocks, []
    raise FileWriteError("write_files 需要 files 清单或 extract 原文")

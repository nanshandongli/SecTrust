"""
内置合规扫描模板库（Built-in Compliance Scan Templates）

每个模板 = 一段**在沙箱内执行**的 Python 脚本（零网络、零外部依赖、只读扫描目标目录）。
扫描结果统一输出一行 JSON：
    {"template": <id>, "scanned_files": N, "findings": [ {file, line, severity, rule, match}, ... ] }

所有模板都经由「审批中心 + 沙箱隔离」双重闸门（见 templates/__init__.py 的编排）。
本文件只负责定义模板与脚本，不包含任何 I/O / 审批逻辑。

信任边界：
- 脚本运行在 sandbox 中（Docker/E2B 强隔离，或本地 jail 降级），不联网、不写外部文件。
- dependency_vuln 是**离线启发式**规则表（少量常见 CVE 的上界版本），并非实时 NVD 查询；
  生产应接入 OSV/NVD 数据源，此处仅作开箱即用的基线。
"""

from . import register, ScanTemplate
from sandbox import safe_scan_command


# ---------------------------------------------------------------------------
# 1) 密钥 / Token 泄露扫描
# ---------------------------------------------------------------------------
SECRET_LEAK_SCRIPT = r'''
import os, re, json, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
ROOT = sys.argv[1] if len(sys.argv) > 1 else "."
SKIP = {".git", ".venv", "node_modules", "__pycache__", ".sandbox", "dist", "build"}
PATTERNS = [
    ("AWS_ACCESS_KEY_ID", re.compile(r"(AKIA|ASIA)[0-9A-Z]{16}")),
    ("AWS_SECRET_ACCESS_KEY", re.compile(r"aws_secret_access_key\s*[:=]\s*['\"][A-Za-z0-9/+=]{40}['\"]")),
    ("PRIVATE_KEY", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----")),
    ("JWT", re.compile(r"eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")),
    ("GOOGLE_API_KEY", re.compile(r"AIza[0-9A-Za-z\-_]{35}")),
    ("SLACK_TOKEN", re.compile(r"xox[baprs]-[0-9A-Za-z\-]{10,}")),
    ("GITHUB_TOKEN", re.compile(r"gh[pousr]_[0-9A-Za-z]{36,}")),
    ("STRIPE_LIVE_KEY", re.compile(r"sk_live_[0-9a-zA-Z]{24,}")),
    ("GENERIC_SECRET", re.compile(r"(?i)(api[_-]?key|secret|token|password|passwd|client_secret)\s*[:=]\s*['\"][A-Za-z0-9/+=_\-!@#$%^&*()]{12,}['\"]")),
]
findings = []
scanned = 0
for dp, dns, fns in os.walk(ROOT):
    if os.path.basename(dp) in SKIP:
        continue
    for fn in fns:
        fp = os.path.join(dp, fn)
        try:
            with open(fp, encoding="utf-8", errors="ignore") as fh:
                text = fh.read()
        except Exception:
            continue
        scanned += 1
        rel = os.path.relpath(fp, ROOT)
        for name, pat in PATTERNS:
            for m in pat.finditer(text):
                ln = text[:m.start()].count("\n") + 1
                snippet = m.group(0)
                if len(snippet) > 40:
                    snippet = snippet[:40] + "..."
                low = snippet.lower()
                # 占位/示例凭据白名单：对所有规则生效（此前只对 GENERIC_SECRET 生效，
                # 导致 AWS 官方文档示例密钥 AKIAIOSFODNN7EXAMPLE 被判为 high 误报）
                if any(k in low for k in ("example", "your", "changeme", "xxxx",
                                          "placeholder", "dummy", "redacted", "<")):
                    continue
                findings.append({"file": rel, "line": ln, "severity": "high", "rule": name, "match": snippet})
print(json.dumps({"template": "secret_leak", "scanned_files": scanned, "findings": findings}, ensure_ascii=False))
'''


# ---------------------------------------------------------------------------
# 2) 硬编码凭证扫描（聚焦配置 / 环境变量文件）
# ---------------------------------------------------------------------------
HARDCODED_CRED_SCRIPT = r'''
import os, re, json, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
ROOT = sys.argv[1] if len(sys.argv) > 1 else "."
SKIP = {".git", ".venv", "node_modules", "__pycache__", ".sandbox", "dist", "build"}
CONFIG_EXT = {".env", ".ini", ".cfg", ".conf", ".yaml", ".yml", ".toml", ".properties", ".json", ".xml"}
PATTERNS = [
    ("DB_PASSWORD", re.compile(r"(?i)(db_?pass\w*|database_?password|mysql_?password|postgres_?password)\s*[:=]\s*['\"]?[^'\"]{3,}")),
    ("PLAINTEXT_PASSWORD", re.compile(r"(?i)(password|passwd|pwd)\s*[:=]\s*['\"][^'\"]{3,}['\"]")),
    ("CONNECTION_STRING_WITH_CREDS", re.compile(r"(?i)(jdbc|mongodb(\+srv)?|postgres(ql)?|mysql|redis|amqp)://[^:\s]+:[^@\s]+@")),
    ("PRIVATE_KEY_IN_CONF", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("CLOUD_CRED_IN_CONF", re.compile(r"(?i)(aws_access_key_id|aws_secret_access_key|gcp_service_account|azure_storage_key)\s*[:=]")),
]
findings = []
scanned = 0
for dp, dns, fns in os.walk(ROOT):
    if os.path.basename(dp) in SKIP:
        continue
    for fn in fns:
        ext = os.path.splitext(fn)[1].lower()
        if ext not in CONFIG_EXT and fn != ".env":
            continue
        fp = os.path.join(dp, fn)
        try:
            with open(fp, encoding="utf-8", errors="ignore") as fh:
                text = fh.read()
        except Exception:
            continue
        scanned += 1
        rel = os.path.relpath(fp, ROOT)
        if fn == ".env":
            findings.append({"file": rel, "line": 0, "severity": "medium", "rule": "COMMITTED_DOTENV", "match": "仓库内发现 .env 文件，可能含敏感配置，不应提交到版本库"})
        for name, pat in PATTERNS:
            for m in pat.finditer(text):
                ln = text[:m.start()].count("\n") + 1
                snippet = m.group(0)
                if len(snippet) > 60:
                    snippet = snippet[:60] + "..."
                low = snippet.lower()
                if "example" in low or "your" in low or "changeme" in low or "xxxx" in low or "<" in low:
                    continue
                findings.append({"file": rel, "line": ln, "severity": "high", "rule": name, "match": snippet})
print(json.dumps({"template": "hardcoded_credential", "scanned_files": scanned, "findings": findings}, ensure_ascii=False))
'''


# ---------------------------------------------------------------------------
# 3) 依赖漏洞扫描（离线启发式，非实时 NVD）
# ---------------------------------------------------------------------------
DEP_VULN_SCRIPT = r'''
import os, re, json, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
ROOT = sys.argv[1] if len(sys.argv) > 1 else "."
SKIP = {".git", ".venv", "node_modules", "__pycache__", ".sandbox", "dist", "build"}
RULES = {
  "npm": {
    "lodash": ("4.17.21", "原型污染 CVE-2020-8203 / CVE-2021-23337"),
    "minimist": ("1.2.6", "原型污染 CVE-2021-44906"),
    "axios": ("0.21.1", "SSRF / 凭证泄露 CVE-2019-10742 等"),
    "tar": ("6.1.9", "路径遍历 CVE-2021-32803"),
    "ws": ("7.4.6", "拒绝服务 CVE-2021-32640"),
    "node-fetch": ("2.6.7", "暴露凭据 CVE-2022-0235"),
  },
  "python": {
    "flask": ("2.2.5", "安全头 / 调试缺陷"),
    "django": ("3.2.18", "多个 CVE 累积修复"),
    "requests": ("2.20.0", "重定向凭据泄露 CVE-2018-18074"),
    "urllib3": ("1.26.5", "CRLF 注入 CVE-2020-26137"),
    "pyyaml": ("5.4", "任意代码执行 CVE-2020-1747"),
    "pillow": ("8.1.1", "多 CVE 累积修复"),
    "jinja2": ("2.11.3", "沙箱绕过 CVE-2020-28493"),
  },
}
def pv(s):
    s = re.sub(r"^[^\d]*", "", s)
    s = re.split(r"[^\d.]+", s)[0] if s else "0"
    parts = []
    for x in s.split("."):
        try:
            parts.append(int(x))
        except Exception:
            parts.append(0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])
def vuln(v, upper):
    try:
        return pv(v) < pv(upper)
    except Exception:
        return False
findings = []
scanned = 0
for dp, dns, fns in os.walk(ROOT):
    if os.path.basename(dp) in SKIP:
        continue
    for fn in fns:
        fp = os.path.join(dp, fn)
        rel = os.path.relpath(fp, ROOT)
        if fn == "package.json":
            try:
                with open(fp, encoding="utf-8", errors="ignore") as fh:
                    data = fh.read()
            except Exception:
                continue
            scanned += 1
            for m in re.finditer(r"\"(lodash|minimist|axios|tar|ws|node-fetch)\"\s*:\s*\"([^\"]+)\"", data):
                pkg, ver = m.group(1), m.group(2).lstrip("^~>=< ")
                if pkg in RULES["npm"] and vuln(ver, RULES["npm"][pkg][0]):
                    findings.append({"file": rel, "line": 0, "severity": "medium",
                                     "rule": "npm:" + pkg + "<" + RULES["npm"][pkg][0],
                                     "match": pkg + "@" + ver + " -> " + RULES["npm"][pkg][1]})
        elif fn in ("requirements.txt", "Pipfile"):
            try:
                with open(fp, encoding="utf-8", errors="ignore") as fh:
                    data = fh.read()
            except Exception:
                continue
            scanned += 1
            for m in re.finditer(r"^\s*([A-Za-z0-9_\-]+)\s*[=<>~!]+\s*([0-9][0-9A-Za-z.\-]*)", data, re.M):
                pkg, ver = m.group(1).lower(), m.group(2)
                if pkg in RULES["python"] and vuln(ver, RULES["python"][pkg][0]):
                    findings.append({"file": rel, "line": 0, "severity": "medium",
                                     "rule": "python:" + pkg + "<" + RULES["python"][pkg][0],
                                     "match": pkg + "@" + ver + " -> " + RULES["python"][pkg][1]})
print(json.dumps({"template": "dependency_vuln", "manifests_scanned": scanned, "findings": findings}, ensure_ascii=False))
'''


# ---------------------------------------------------------------------------
# 4) CI/CD 配置安全检查
# ---------------------------------------------------------------------------
CICD_SCRIPT = r'''
import os, re, json, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
ROOT = sys.argv[1] if len(sys.argv) > 1 else "."
SKIP = {".git", ".venv", "node_modules", "__pycache__", ".sandbox", "dist", "build"}
TARGETS = []
for dp, dns, fns in os.walk(ROOT):
    if os.path.basename(dp) in SKIP:
        continue
    for fn in fns:
        low = fn.lower()
        if low in (".gitlab-ci.yml", "jenkinsfile", "bitbucket-pipelines.yml") or low.endswith(".yml") or low.endswith(".yaml"):
            TARGETS.append(os.path.join(dp, fn))
CHECKS = [
    ("PULL_REQUEST_TARGET", re.compile(r"pull_request_target"), "high",
     "使用了 pull_request_target，可能在不受信任的 PR 中暴露仓库 Secrets，需极其谨慎"),
    ("PERMISSIONS_WRITE_ALL", re.compile(r"permissions\s*:\s*(write|write-all)", re.I), "medium",
     "授予了过宽的写权限（write / write-all）"),
    ("PIPED_TO_SHELL", re.compile(r"(curl|wget)[^\n]*\|\s*(sh|bash|python)"), "high",
     "从网络下载脚本并直接执行（curl|sh），存在供应链投毒风险"),
    ("CHECKOUT_UNTRUSTED_REF", re.compile(r"ref\s*:\s*\${{\s*github\.event\.pull_request\.head\.ref"), "high",
     "checkout 了 PR 头部分支（不可信代码），若配合 secrets 使用风险极高"),
    ("SECRET_ECHO", re.compile(r"echo[^\n]*\$\{\{\s*secrets\."), "low",
     "在 run 步骤中打印 secrets，可能泄露到构建日志"),
]
findings = []
scanned = 0
for fp in TARGETS:
    try:
        with open(fp, encoding="utf-8", errors="ignore") as fh:
            text = fh.read()
    except Exception:
        continue
    scanned += 1
    rel = os.path.relpath(fp, ROOT)
    if not re.search(r"timeout-minutes", text, re.I):
        findings.append({"file": rel, "line": 0, "severity": "low", "rule": "NO_TIMEOUT_MINUTES",
                         "match": "CI 作业未设置 timeout-minutes，可能挂起并消耗资源"})
    for name, pat, sev, msg in CHECKS:
        for m in pat.finditer(text):
            ln = text[:m.start()].count("\n") + 1
            findings.append({"file": rel, "line": ln, "severity": sev, "rule": name, "match": msg})
print(json.dumps({"template": "cicd_config", "configs_scanned": scanned, "findings": findings}, ensure_ascii=False))
'''


# ---------------------------------------------------------------------------
# 注册内置模板
# ---------------------------------------------------------------------------
register(ScanTemplate(
    id="generic_sast",
    name="代码卫生基线扫描",
    description="低强度 SAST 基线：统计文件/代码行数、TODO/FIXME 标记、疑似硬编码密钥。",
    severity="low",
    command=lambda w: safe_scan_command(w),  # 复用 sandbox 的通用扫描脚本
))

register(ScanTemplate(
    id="secret_leak",
    name="密钥 / Token 泄露扫描",
    description="扫描源码中的真实密钥形态：AWS/GCP/Slack/GitHub/Stripe Key、JWT、私钥、通用 api_key/secret/token 赋值。",
    severity="high",
    script=SECRET_LEAK_SCRIPT,
))

register(ScanTemplate(
    id="hardcoded_credential",
    name="硬编码凭证扫描",
    description="聚焦配置文件 / .env：数据库密码、连接串内嵌凭据、云凭证、提交的 .env 文件。",
    severity="high",
    script=HARDCODED_CRED_SCRIPT,
))

register(ScanTemplate(
    id="dependency_vuln",
    name="依赖漏洞扫描（离线启发式）",
    description="解析 package.json / requirements.txt / Pipfile，对照内置常见 CVE 上界版本表给出告警（离线基线，非实时 NVD）。",
    severity="medium",
    script=DEP_VULN_SCRIPT,
))

register(ScanTemplate(
    id="cicd_config",
    name="CI/CD 配置安全检查",
    description="检查 .github/workflows、.gitlab-ci.yml、Jenkinsfile 等：pull_request_target、过宽权限、curl|sh、checkout 不受信分支、未设超时等。",
    severity="medium",
    script=CICD_SCRIPT,
))

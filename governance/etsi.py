"""
etsi.py —— AI 计算平台安全（ETSI TS 104 033）：应用层就绪 + 部署层交付物

覆盖（应用层可交付部分）：
  · hardening_manifest()  —— systemd / Docker / seccomp 加固清单与模板
  · runtime_self_check()  —— 运行时自检（探测不到的项标 unknown+reason，不伪造全绿）
  · LogIntegrityChain     —— 审计日志哈希链（prev_hash 串联 sha256，篡改可检出）
  · attestation_status()  —— 硬件/固件证明状态（**恒不自证**）

诚实边界（必须遵守，违反即"伪合规"）：
  · 硬件固件校验 / TPM / 安全启动 / OS 级强隔离属于**部署层**，需要 TPM/UEFI
    固件 + 第三方 attestation 服务；应用层代码**无法自证**，attestation_status()
    永远返回 attested=False，绝不返回 true。
  · runtime_self_check() 只能探测"进程自身的可见事实"；探测不到一律
    unknown + reason，不猜、不补绿。overall 绝不在有 unknown 时返回 pass。
  · 哈希链是**防篡改检测**（tamper-evident），不是防删除（tamper-proof）；
    要防删除仍需日志外发 / WORM 存储（见 hardening_manifest）。
  · 本模块是"可验证、经得起测"的交付物，不是 ETSI 认证结论；认证需第三方机构。

零第三方依赖（仅标准库）。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from typing import Dict, List, Optional

DEFAULT_CHAIN_PATH = os.path.join("data", "audit_chain.jsonl")
GENESIS_HASH = "0" * 64

STANDARD = "ETSI TS 104 033"


# --------------------------------------------------------------------------
# 1) 加固清单 / 部署模板
# --------------------------------------------------------------------------
def hardening_manifest() -> Dict:
    """输出可直接落地的加固清单（systemd unit / Docker / seccomp / 权限 / 日志）。"""

    systemd_unit = """# /etc/systemd/system/sectrust.service —— 最小权限运行模板
[Unit]
Description=SecTrust 协作审计台（AI 同事平台）
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=sectrust
Group=sectrust
WorkingDirectory=/opt/sectrust
ExecStart=/opt/sectrust/venv/bin/python -m execution.daemon
Restart=on-failure
RestartSec=5

# —— 权限收敛（ETSI：最小权限 / 特权提升防护）——
NoNewPrivileges=true
CapabilityBoundingSet=
AmbientCapabilities=
ProtectSystem=strict          # 根文件系统只读（除 ReadWritePaths）
ProtectHome=true
PrivateTmp=true
PrivateDevices=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true
MemoryDenyWriteExecute=true
SystemCallArchitectures=native
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
ReadWritePaths=/opt/sectrust/data     # 唯一可写目录

# —— 资源与审计 ——
LimitNOFILE=4096
StandardOutput=append:/var/log/sectrust/daemon.log
StandardError=append:/var/log/sectrust/daemon.error.log

[Install]
WantedBy=multi-user.target
"""

    return {
        "standard": STANDARD,
        "scope": "应用层就绪 + 部署层建议（应用不自证硬件/固件）",
        "systemd_unit": systemd_unit,
        "docker": {
            "read_only_rootfs": True,
            "user": "10001:10001",
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true",
                             "seccomp=/etc/sectrust/seccomp-profile.json"],
            "tmpfs": ["/tmp:rw,noexec,nosuid,size=64m"],
            "read_write_mounts": ["/opt/sectrust/data"],
            "pids_limit": 256,
            "note": "容器镜像需固定 digest（禁止 latest 漂移）；构建期扫描 CVE。",
        },
        "seccomp": {
            "default_action": "SCMP_ACT_ERRNO",
            "allow_minimum": ["read", "write", "openat", "close", "fstat", "mmap",
                              "mprotect", "brk", "rt_sigaction", "rt_sigprocmask",
                              "exit", "exit_group", "futex", "clock_gettime",
                              "getrandom", "epoll_wait", "epoll_ctl", "socket",
                              "connect", "recvfrom", "sendto"],
            "deny": ["ptrace", "process_vm_writev", "mount", "umount2", "kexec_load",
                     "bpf", "perf_event_open", "keyctl", "add_key", "request_key"],
            "note": "按实际 workload 收敛；profile 需随系统调用白名单一同评审、版本化。",
        },
        "least_privilege": {
            "run_user": "sectrust（非 root，无 shell，UID>=1000）",
            "data_dir": "仅 data/ 可写，属主 sectrust:sectrust，mode 0700",
            "credentials": "API Key 由代理层注入（凭据隔离），不落盘明文、不进镜像",
            "sudo": "运行用户禁止 sudo / 特权组",
        },
        "readonly_rootfs": {
            "enabled": True,
            "writable_paths": ["data/"],
            "note": "根文件系统只读 + 仅数据目录可写；升级走新镜像滚动替换，不原地改代码。",
        },
        "log_forwarding": {
            "target": "外部只追加（append-only）/ WORM 日志系统",
            "env": "SECTRUST_LOG_ENDPOINT",
            "note": ("哈希链检测篡改但不防整段删除；ETSI 建议审计日志实时外发到"
                     "独立信任域，链根哈希定期归档。"),
        },
        "network": {
            "egress_allowlist": "仅放行已登记的模型/MCP/联邦对端域名（见 governance.kb_control / interop）",
            "note": "默认拒绝出站；出域审批 fail-closed（governance.kb_control）。",
        },
        "certification_notice": (
            "本清单是部署建议，不是 ETSI 合规认证结论。最终合规性须由具备资质的"
            "第三方检测/认证机构评定；硬件固件校验 / TPM / 安全启动需部署层与"
            "第三方 attestation 服务支持，应用不自证。"
        ),
    }


# --------------------------------------------------------------------------
# 2) 运行时自检（探测不到 = unknown + reason，绝不伪造全绿）
# --------------------------------------------------------------------------
def _chk(item: str, status: str, detail: str) -> Dict:
    return {"item": item, "status": status, "detail": detail}


def runtime_self_check(project_root: str = ".") -> Dict:
    """探测当前进程的可见部署事实。status ∈ pass/fail/unknown。

    overall：有 fail → "fail"；有 unknown → "unknown"；全 pass → "pass"。
    绝不因"没报错"就判定通过——探测不到必须标 unknown+reason。
    """
    checks: List[Dict] = []

    # (1) 特权用户
    try:
        if os.name == "nt":
            import ctypes
            elevated = bool(ctypes.windll.shell32.IsUserAnAdmin())  # noqa: S
            checks.append(_chk(
                "non_root_user", "fail" if elevated else "pass",
                "Windows：当前进程%s管理员权限" % ("具备" if elevated else "非")))
        else:
            root = (os.geteuid() == 0)
            checks.append(_chk(
                "non_root_user", "fail" if root else "pass",
                "POSIX：euid=%d（%s root）" % (os.geteuid(), "是" if root else "非")))
    except Exception as e:  # noqa: BLE001
        checks.append(_chk("non_root_user", "unknown",
                           "无法探测特权状态：%s" % e))

    # (2) /proc 可用性（Linux 强隔离/状态探测的前提）
    if os.name == "nt":
        checks.append(_chk("proc_filesystem", "unknown",
                           "Windows 无 /proc，Linux 部署项不适用，未验证"))
    elif os.path.isdir("/proc/self"):
        checks.append(_chk("proc_filesystem", "pass", "/proc/self 可用"))
    else:
        checks.append(_chk("proc_filesystem", "unknown",
                           "/proc/self 不可用，无法读取内核隔离状态"))

    # (3) data/ 目录权限（最小权限的数据目录）
    data_dir = os.path.join(project_root, "data")
    try:
        if not os.path.isdir(data_dir):
            checks.append(_chk("data_dir_permissions", "unknown",
                               "data/ 不存在，尚未初始化，无法判定权限"))
        elif os.name == "nt":
            checks.append(_chk("data_dir_permissions", "unknown",
                               "Windows ACL 无法用 POSIX mode 判定；需部署层用 icacls 核验"))
        else:
            mode = os.stat(data_dir).st_mode & 0o777
            world = mode & 0o077
            checks.append(_chk(
                "data_dir_permissions", "fail" if world else "pass",
                "data/ mode=%o（%s）" % (mode, "组/其他用户可写" if world else "仅属主可写")))
    except Exception as e:  # noqa: BLE001
        checks.append(_chk("data_dir_permissions", "unknown", "stat 失败：%s" % e))

    # (4) 沙箱隔离后端（能 import ≠ 隔离生效，只报可探测事实）
    try:
        from sandbox import SandboxManager  # noqa: F401
        try:
            mgr = SandboxManager.auto()
            backend = str(getattr(mgr, "backend", None) or getattr(
                mgr, "name", None) or "unknown")
            checks.append(_chk("sandbox_isolation", "unknown",
                               "沙箱模块可用（backend=%s）；是否强制隔离需部署层验证，"
                               "应用不自证" % backend))
        except Exception as e:  # noqa: BLE001
            checks.append(_chk("sandbox_isolation", "unknown",
                               "沙箱模块存在但 auto() 探测失败：%s" % e))
    except Exception as e:  # noqa: BLE001
        checks.append(_chk("sandbox_isolation", "unknown", "沙箱模块不可导入：%s" % e))

    # (5) seccomp 状态（Linux /proc/self/status 的 Seccomp 字段）
    if os.name == "nt":
        checks.append(_chk("seccomp", "unknown",
                           "Windows 无 seccomp；等价机制需部署层（AppContainer/WDAC）验证"))
    else:
        try:
            seccomp = None
            with open("/proc/self/status", "r", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("Seccomp:"):
                        seccomp = line.split(":", 1)[1].strip()
                        break
            if seccomp is None:
                checks.append(_chk("seccomp", "unknown",
                                   "/proc/self/status 无 Seccomp 字段（内核不支持？）"))
            elif seccomp == "0":
                checks.append(_chk("seccomp", "fail", "Seccomp=0（未启用沙箱过滤）"))
            else:
                checks.append(_chk("seccomp", "pass", "Seccomp=%s（已启用过滤）" % seccomp))
        except Exception as e:  # noqa: BLE001
            checks.append(_chk("seccomp", "unknown", "读取 seccomp 状态失败：%s" % e))

    # (6) 只读根文件系统：应用层无法可靠探测（写探针有副作用），明确 unknown
    checks.append(_chk("readonly_rootfs", "unknown",
                       "应用无法可靠探测根文件系统挂载选项；需部署层用 "
                       "findmnt -no OPTIONS / 或 systemd-analyze security 核验"))

    # (7) 日志外发
    endpoint = os.environ.get("SECTRUST_LOG_ENDPOINT", "")
    checks.append(_chk(
        "log_forwarding", "unknown" if not endpoint else "unknown",
        ("未配置 SECTRUST_LOG_ENDPOINT，审计日志仅本地；ETSI 建议外发到独立信任域"
         if not endpoint else
         "已配置外发端点（%s…），但应用无法自证对端为 WORM/只追加，需部署层验证"
         % endpoint[:24])))

    statuses = {c["status"] for c in checks}
    if "fail" in statuses:
        overall = "fail"
    elif "unknown" in statuses:
        overall = "unknown"
    else:
        overall = "pass"
    return {
        "standard": STANDARD,
        "checks": checks,
        "overall": overall,
        "generated_at": time.time(),
        "honest_notice": ("overall 含 unknown 表示该项无法由应用自证，需部署层/第三方"
                          "核验；本自检绝不因'没报错'就判定合规。"),
    }


# --------------------------------------------------------------------------
# 3) 审计日志哈希链（tamper-evident）
# --------------------------------------------------------------------------
def _canonical(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      default=str, separators=(",", ":"))


def _entry_hash(prev: str, seq: int, at: float, entry) -> str:
    payload = "%s|%d|%s|%s" % (prev, seq, at, _canonical(entry))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class LogIntegrityChain:
    """审计日志哈希链：每条记录 hash = sha256(prev_hash | seq | at | entry)。

    append_log() 追加一行 JSONL（data/audit_chain.jsonl）；verify_chain() 逐行
    重算并校验 prev_hash 链与 seq 连续性，任何篡改/断链都能定位到 seq。

    诚实边界：这是**篡改检测**不是防删除；整段删除需靠日志外发/WORM 存储。
    并发：单进程内用锁保护；跨进程追加需外部文件锁或集中式日志服务。
    """

    def __init__(self, path: str = DEFAULT_CHAIN_PATH):
        self.path = path
        self._lock = threading.RLock()

    def _read_lines(self) -> List[str]:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                return [ln for ln in fh.read().splitlines() if ln.strip()]
        except FileNotFoundError:
            return []
        except OSError as e:
            raise RuntimeError("审计链读取失败：%s" % e)

    def append_log(self, entry: Dict) -> Dict:
        with self._lock:
            lines = self._read_lines()
            seq = len(lines)
            prev = GENESIS_HASH
            if lines:
                try:
                    prev = json.loads(lines[-1]).get("hash") or GENESIS_HASH
                except (ValueError, AttributeError):
                    prev = GENESIS_HASH
            at = time.time()
            rec = {"seq": seq, "at": at, "prev_hash": prev,
                   "entry": entry, "hash": _entry_hash(prev, seq, at, entry)}
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
            return rec

    def verify_chain(self) -> Dict:
        """逐行重算 hash 与 prev_hash 链；返回 {ok, checked, errors, broken_at}。"""
        errors: List[str] = []
        broken_at: Optional[int] = None
        try:
            lines = self._read_lines()
        except RuntimeError as e:
            return {"ok": False, "checked": 0, "errors": [str(e)], "broken_at": None}
        prev = GENESIS_HASH
        for i, line in enumerate(lines):
            try:
                rec = json.loads(line)
            except ValueError:
                errors.append("第 %d 行不是合法 JSON" % (i + 1))
                broken_at = broken_at or i
                return {"ok": False, "checked": len(lines), "errors": errors,
                        "broken_at": broken_at}
            seq = rec.get("seq")
            if seq != i:
                errors.append("seq 不连续：期望 %d，实际 %r" % (i, seq))
                broken_at = broken_at if broken_at is not None else i
            if rec.get("prev_hash") != prev:
                errors.append("seq=%r prev_hash 断链（前序哈希被改或记录被删）" % seq)
                broken_at = broken_at if broken_at is not None else i
            expected = _entry_hash(rec.get("prev_hash") or "", rec.get("seq") or 0,
                                   rec.get("at") or 0.0, rec.get("entry"))
            if expected != rec.get("hash"):
                errors.append("seq=%r hash 不匹配（entry 内容或 at 被篡改）" % seq)
                broken_at = broken_at if broken_at is not None else i
            prev = rec.get("hash") or prev
        return {"ok": not errors, "checked": len(lines),
                "errors": errors, "broken_at": broken_at}

    def tail(self, n: int = 10) -> List[Dict]:
        out = []
        for line in self._read_lines()[-n:]:
            try:
                out.append(json.loads(line))
            except ValueError:
                out.append({"raw": line, "parse_error": True})
        return out


# 模块级便捷实例 / 函数
_default_chain = LogIntegrityChain()


def append_log(entry: Dict, path: str = DEFAULT_CHAIN_PATH) -> Dict:
    return LogIntegrityChain(path).append_log(entry)


def verify_chain(path: str = DEFAULT_CHAIN_PATH) -> Dict:
    return LogIntegrityChain(path).verify_chain()


# --------------------------------------------------------------------------
# 4) 硬件证明状态 —— 恒不自证
# --------------------------------------------------------------------------
def attestation_status() -> Dict:
    """硬件/固件证明状态：应用层**永远不返回 attested=True**。

    TPM 度量 / UEFI 安全启动 / 固件完整性需要部署层硬件支持与第三方
    attestation 服务；本机代码无法验证，更不能自己给自己背书。
    """
    return {
        "standard": STANDARD,
        "attested": False,
        "hardware_root_of_trust": "unknown",
        "tpm_measured_boot": "unknown",
        "secure_boot": "unknown",
        "firmware_integrity": "unknown",
        "reason": ("需 TPM/UEFI 固件支持与第三方 attestation 服务；应用层无法自证，"
                   "绝不返回 attested=True。"),
        "next_step": ("部署层启用 TPM 度量启动 + 安全启动，接入第三方远程证明服务，"
                      "由其对引用值（reference values）签名背书。"),
    }


def describe(project_root: str = ".") -> Dict:
    """供控制台 /api/governance/etsi 一次性展示的聚合视图。"""
    return {
        "standard": STANDARD,
        "hardening_manifest": hardening_manifest(),
        "runtime_self_check": runtime_self_check(project_root=project_root),
        "log_integrity": verify_chain(),
        "attestation": attestation_status(),
        "honest_notice": ("应用层提供加固清单/运行时自检/日志完整性；硬件固件校验与"
                          "OS 级强隔离属部署层，需 TPM/UEFI + 第三方评定，应用不自证。"),
    }

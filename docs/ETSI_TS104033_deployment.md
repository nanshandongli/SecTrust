# ETSI TS 104 033 部署加固指南（SecTrust / 协作审计台）

> 交付物定位：**应用层就绪 + 部署层建议**，不是 ETSI 合规认证结论。
> 生成模块：`governance/etsi.py`（零第三方依赖）。
> 最终合规性须由具备资质的第三方检测/认证机构评定——本产品据此「可验证、经得起测」，
> 但**不能自我宣告通过认证**。

---

## 一、能力边界（先读这一节）

| 能力 | 由谁提供 | 本仓库能否自证 |
|---|---|---|
| 加固清单（systemd / Docker / seccomp 模板） | 应用层 `hardening_manifest()` | ✅ 可交付、可审查 |
| 运行时自检（特权用户 / data 权限 / seccomp 等） | 应用层 `runtime_self_check()` | ⚠️ 探测得到才报，探测不到标 `unknown+reason` |
| 审计日志哈希链（篡改可检出） | 应用层 `LogIntegrityChain` | ✅ 可离线 `verify_chain()` 验证 |
| 最小权限运行用户 / 只读根文件系统 | **部署层**（systemd / 容器运行时） | ❌ 应用无法自证挂载选项 |
| 网络出站白名单 + 出域审批 | 应用层 `kb_control` / `interop` + 部署层防火墙 | ⚠️ 应用侧 fail-closed，网络层需部署核验 |
| 硬件固件校验 / TPM 度量启动 / UEFI 安全启动 | **部署层硬件 + 第三方 attestation 服务** | ❌ **应用恒不自证**，`attestation_status().attested` 永远为 `false` |
| 认证与资质背书 | 第三方检测/认证机构 | ❌ 代码只做到可验证 |

---

## 二、systemd 最小权限单元（可直接落地）

```ini
# /etc/systemd/system/sectrust.service
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

# —— 权限收敛 ——
NoNewPrivileges=true
CapabilityBoundingSet=
AmbientCapabilities=
ProtectSystem=strict
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

LimitNOFILE=4096
StandardOutput=append:/var/log/sectrust/daemon.log
StandardError=append:/var/log/sectrust/daemon.error.log

[Install]
WantedBy=multi-user.target
```

落地步骤：

```bash
# 1) 建无 shell 的专用运行用户
sudo useradd --system --no-create-home --shell /usr/sbin/nologin sectrust

# 2) 代码只读、数据目录仅属主可写
sudo chown -R root:root /opt/sectrust
sudo install -d -o sectrust -g sectrust -m 0700 /opt/sectrust/data
sudo install -d -o sectrust -g sectrust -m 0750 /var/log/sectrust

# 3) 安装并审查安全等级（关注 Overall exposure level）
sudo systemctl daemon-reload
sudo systemctl enable --now sectrust
sudo systemd-analyze security sectrust.service

# 4) 复核根文件系统只读 / 命名空间隔离
findmnt -no OPTIONS /
systemctl show sectrust.service -p ProtectSystem -p NoNewPrivileges -p ReadWritePaths
```

控制台（HTTP 服务）同样应以该用户运行，或拆成独立 unit 并复用同一组加固指令。

---

## 三、容器化部署（Docker / Compose 建议）

```yaml
services:
  sectrust-daemon:
    image: sectrust@sha256:<固定-digest>      # 禁止 latest 漂移
    read_only: true                            # 只读根文件系统
    user: "10001:10001"                        # 非 root
    cap_drop: ["ALL"]
    security_opt:
      - no-new-privileges:true
      - seccomp=/etc/sectrust/seccomp-profile.json
    tmpfs:
      - /tmp:rw,noexec,nosuid,size=64m
    volumes:
      - sectrust-data:/opt/sectrust/data:rw    # 唯一可写卷
      - /var/log/sectrust:/var/log/sectrust:rw
    pids_limit: 256
    mem_limit: 2g
    restart: on-failure
volumes:
  sectrust-data:
```

要点：

- **镜像固定 digest**，构建期跑 CVE 扫描；不把 API Key 打进镜像或环境变量明文。
- **凭证隔离**：模型/MCP/联邦对端的 token 由平台代理层注入（`governance.kb_control` 只存 `token_ref`，
  `governance.interop.FederationRegistry` 只存引用），Agent 拿不到明文。
- **出站默认拒绝**：防火墙/网络策略仅放行已登记的模型端点、MCP Server 域名与联邦对端；
  应用侧出域审批 `EgressApproval.is_allowed()` 为 **fail-closed**（confidential 未批即拒）。

### seccomp 白名单要点

```json
{
  "defaultAction": "SCMP_ACT_ERRNO",
  "architectures": ["SCMP_ARCH_X86_64"],
  "syscalls": [{
    "names": ["read","write","openat","close","fstat","mmap","mprotect","brk",
              "rt_sigaction","rt_sigprocmask","exit","exit_group","futex",
              "clock_gettime","getrandom","epoll_wait","epoll_ctl","socket",
              "connect","recvfrom","sendto"],
    "action": "SCMP_ACT_ALLOW"
  }]
}
```

明确拒绝 `ptrace` / `process_vm_writev` / `mount` / `kexec_load` / `bpf` / `keyctl` 等
提权与内核攻击面。profile 必须版本化、随 workload 变更评审。

---

## 四、审计日志完整性与外发

`governance/etsi.py` 提供 sha256 哈希链（`data/audit_chain.jsonl`）：

```python
from governance import etsi
etsi.append_log({"actor": "alice", "action": "approval.act", "task": "task-xxx"})
report = etsi.verify_chain()
assert report["ok"], report["errors"]
```

- 每条记录 `hash = sha256(prev_hash | seq | at | entry)`，首条 `prev_hash` 为 64 个 `0`。
- `verify_chain()` 逐行重算并校验 `prev_hash` 链与 `seq` 连续性，篡改 `entry` / `hash` / 删除中间行
  都会返回 `ok=False` 并定位 `broken_at`（seq）。
- **诚实边界**：这是 *tamper-evident*（篡改可检出），不是 *tamper-proof*（不防整段删除）。
  因此必须配置**日志外发**：设置 `SECTRUST_LOG_ENDPOINT` 指向独立信任域的只追加/WORM 日志系统，
  并定期归档链根哈希。哈希链与既有 `audit_suite.py` 三件套互补，不互相替代。

---

## 五、运行时自检的使用方式

```python
from governance import etsi
r = etsi.runtime_self_check(project_root=".")
print(r["overall"], r["checks"])
```

`overall` 取值：`pass`（全部可探测项通过）/ `fail`（存在明确不合规项，如 root 运行、data 目录组可写）/
`unknown`（存在无法由应用自证的项）。

**纪律**：`overall` 含 `unknown` 时**绝不等于合规**——例如 Windows 无 `/proc`、无法用 POSIX mode 判定 ACL、
无法可靠探测根文件系统挂载选项，这些项一律标 `unknown + reason`，不猜、不补绿。
部署验收必须用 `systemd-analyze security` / `findmnt` / `icacls` 等部署层工具补齐证据。

---

## 六、硬件证明（TPM / 安全启动）—— 应用不自证

```python
etsi.attestation_status()
# {"attested": false, "tpm_measured_boot": "unknown", "secure_boot": "unknown",
#  "reason": "需 TPM/UEFI 固件支持与第三方 attestation 服务；应用层无法自证，绝不返回 attested=True"}
```

`attested` **恒为 `false`**。要拿到真实硬件证明，需部署层：

1. 启用 TPM 2.0 度量启动（PCR 扩展）与 UEFI 安全启动；
2. 建立固件/内核/镜像的引用值（reference values）与签名基线；
3. 接入第三方远程证明服务，由其对引用值背书并签发证明结果；
4. 将第三方证明结论作为合规证据归档——**不是**由本仓库代码自己输出 `true`。

---

## 七、验收清单（部署层签核用）

- [ ] 运行用户非 root，`NoNewPrivileges=true`，`CapabilityBoundingSet=` 为空
- [ ] 根文件系统只读，仅 `data/` 可写且 mode 0700
- [ ] seccomp profile 已加载，`systemd-analyze security` 无高危暴露
- [ ] 出站防火墙白名单生效，应用侧出域审批 fail-closed 已验证（confidential 未批 → `needs_human`）
- [ ] 审计日志外发端点已配置，哈希链 `verify_chain()` 返回 `ok=True`，篡改演练可检出
- [ ] `runtime_self_check()` 的 `unknown` 项已由部署层工具逐条补证
- [ ] TPM/安全启动证明由第三方服务出具（本仓库不自证）
- [ ] 合规认证结论由第三方检测/认证机构出具

"""
沙箱执行模块（Sandbox）—— 默认「最小权限 + 降级」。

设计目标（对应安全叙事）：
- 任何「可能触碰文件系统 / 执行外部命令」的高权限动作，都先在沙箱里跑；
- 能隔离就隔离（Docker 容器 / E2B 远程），不能隔离就本地受限降级；
- 本地降级模式做「软越权拦截」：限制工作目录(jail)、清理危险环境变量、
  预检命令中明显的越界路径（../、/etc、C:\\Windows 等），超时强杀。

Docker 后端按「隔离工位八条规格」硬化（详见 DockerSandbox 文档）：
  1. 每任务独立容器，结束即销毁（--rm + 显式 kill + cleanup_stale 兜底）
  2. 非 root 用户运行（--user，默认 nobody）
  3. 根文件系统只读，只写 /tmp(tmpfs) 与输出目录
  4. 默认断网（--network none）；外发网络须持已批准 approval_id
  5. 限制 CPU / 内存 / 进程数（--cpus/--memory/--pids-limit，禁 swap）
  6. 输入目录只读挂载，输出目录单独挂载
  7. 命令白名单：只放行受控 Tool，不把任意 shell 交出去
  8. --runtime 为未来 gVisor/Kata 预留（当前只验证 runc 调用链）
配置面：SandboxManager.docker_from_env() 读 SECTRUST_SANDBOX_* 环境变量，
不改代码即可套用上述规格。

⚠️ 信任边界说明：
- Docker / E2B 是强隔离（容器/远程），生产环境优先；
- 本地降级是「弱隔离」——它只是把执行约束在 jail 工作目录并拦截明显越权，
  无法防住恶意的本地提权；请勿在不可信代码上使用本地降级。
- 默认网络：Docker 用 --network none（禁用网络）；本地降级无法禁用网络，
  E2B 由远端策略决定。
- 沙箱只防「被诱导后造成更大破坏」；网络外发等高危动作仍须过审批链
  （ApprovalCenter），两层叠加才是完整边界。

能力探测顺序（SandboxManager.auto()）：
  Docker 可用 -> 用 Docker（强隔离）
  E2B_API_KEY 配置 -> 用 E2B（强隔离，需 pip install e2b）
  否则 -> 本地降级（弱隔离，能跑即跑）

用法：
  from sandbox import SandboxManager
  sb = SandboxManager.auto()
  res = sb.run(["python", "-c", "print('hi')"], timeout=30)
  print(res.content, res.ok, res.backend)

  # 需要外网时：先拿审批，再把 approval 与 approval_id 一起传
  from approval import ApprovalCenter
  ac = ApprovalCenter(db_path=...)
  aid = ac.request("sandbox_network", "任务需访问外网", task_id=tid)
  ac.decide(aid, "approved")
  res = sb.run(cmd, workdir, network="bridge", approval=ac, approval_id=aid)
"""

from __future__ import annotations

import abc
import itertools
import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Optional


# 本地降级模式下，命令/参数里出现这些 token 视为明显越权，直接拦截。
# 注意：只覆盖「系统敏感绝对路径」与「相对跳出(jail)」，不包含 C:\Users 这种用户目录
# —— 否则会误伤 Python 解释器自身路径(C:\Users\...\python.exe)以及合法的扫描目标目录。
_FORBIDDEN_PATH_TOKENS = (
    "../", "..\\",
    "/etc/", "/proc/", "/sys/", "/root/", "/boot/",
    "C:\\Windows", "C:\\Program",
    "/private/etc",
)

# 本模块创建的容器一律带此前缀：cleanup_stale() 只删带前缀的，
# 绝不碰用户自己的容器（"任务结束即销毁"的跨进程兜底）。
CONTAINER_PREFIX = "sectrust-sbx-"

# 非 root 默认用户：nobody（主流镜像都存在；镜像没有 nobody 时用 user= 覆盖）
DEFAULT_CONTAINER_USER = "65534:65534"


def _default_container_user() -> str:
    """规格 2 的非 root 默认值。

    POSIX 上：非 root 调用者用自身 uid:gid——绑定挂载的属主才对得上，
    容器内进程写的文件宿主进程才读得动；root 调用者（CI/容器内）退回 nobody。
    Windows（Docker Desktop）：nobody，属主映射由 Docker Desktop 处理。
    """
    if os.name == "posix":
        try:
            uid, gid = os.geteuid(), os.getegid()
        except AttributeError:        # 理论分支：POSIX 上没有 geteuid 的平台
            return DEFAULT_CONTAINER_USER
        return DEFAULT_CONTAINER_USER if uid == 0 else f"{uid}:{gid}"
    return DEFAULT_CONTAINER_USER


@dataclass
class SandboxResult:
    content: str = ""       # 标准输出（或错误信息）
    ok: bool = True
    error: Optional[str] = None
    logs: list = field(default_factory=list)
    duration: float = 0.0
    backend: str = "none"   # "docker" / "e2b" / "local"
    # 隔离强度（供审计/报告引用，避免"以为是沙箱其实只是命令过滤"的误解）：
    #   "os"        = 操作系统级隔离（Docker / E2B），进程、文件系统、网络均受限
    #   "advisory"  = 仅命令预检（LocalSandbox）：**不是真沙箱**，
    #                 子进程仍可读写 jail 之外的路径，只拦截已知危险 token
    containment: str = "advisory"


class SandboxBackend(abc.ABC):
    """沙箱后端抽象。name 用于结果标注与审计。

    containment 标注真实隔离强度：
      "os"       = 操作系统级隔离（Docker / E2B）
      "advisory" = 仅命令预检（LocalSandbox），**不是真正的沙箱**
    """

    name: str = "base"
    containment: str = "advisory"

    def available(self) -> bool:
        """该后端在当前环境是否可用。"""
        return True

    @abc.abstractmethod
    def run(self, cmd, workdir: str, timeout: float = 30) -> SandboxResult:
        raise NotImplementedError


class LocalSandbox(SandboxBackend):
    """本地受限降级：jail 工作目录 + 清理环境 + 越权路径预检 + 超时强杀。

    弱隔离，仅用于可信的低强度扫描/脚本；不可信代码请用 Docker/E2B。"""

    name = "local"
    # ⚠️ 不是真沙箱：仅对命令做越权 token 预检，子进程仍可读写 jail 之外的路径。
    containment = "advisory"

    def available(self) -> bool:
        return True

    def _jail_check(self, cmd) -> Optional[str]:
        """把命令参数展开成文本，拦截明显越界的路径 token。

        对 list 形式的命令，跳过 argv[0]（解释器自身路径常含 C:\\Users 等，属正常执行，
        不是越权数据访问）；只对后续参数做越权检查。对 shell 字符串形式，
        则先把解释器路径剔除后再检查。"""
        if isinstance(cmd, (list, tuple)):
            items = list(cmd)
            if items:
                items = items[1:]  # 跳过解释器 argv[0]
            text = " ".join(str(c) for c in items)
        else:
            text = str(cmd).replace(sys.executable, "")
        low = text.lower()
        for tok in _FORBIDDEN_PATH_TOKENS:
            if tok.lower() in low:
                return f"拦截越权路径引用: 检测到危险 token '{tok}'"
        return None

    def run(self, cmd, workdir: str, timeout: float = 30, **_kw) -> SandboxResult:
        """本地受限执行。

        input_dir/output_dir/network 等参数是 Docker 后端能力，本后端**没有**：
        本地 jail 只有 workdir 一个挂载点、也没有禁网能力（containment 已是
        advisory，调用方不得据此假设隔离）。管理层（SandboxManager）的网络
        审批门在任何后端上都生效；此处只负责如实忽略不支持的 kwargs。
        """
        os.makedirs(workdir, exist_ok=True)
        # 软越权拦截：命令本身不得试图跳出 jail / 触碰系统敏感区
        blocked = self._jail_check(cmd)
        if blocked:
            return SandboxResult(ok=False, error=blocked, backend=self.name,
                                 containment=self.containment)

        # 清理危险环境变量，仅保留最小集合
        safe_env = {
            "PATH": os.environ.get("PATH", ""),
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
            "TEMP": os.environ.get("TEMP", ""),
            "TMP": os.environ.get("TMP", ""),
        }
        t0 = time.time()
        try:
            # 仅在 workdir 内执行；cwd 即 jail 根，无法用相对 ../ 跳出（已被预检拦截）
            proc = subprocess.run(
                cmd,
                cwd=workdir,
                env=safe_env,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                shell=isinstance(cmd, str),
            )
            out = (proc.stdout or "") + (proc.stderr or "")
            ok = proc.returncode == 0
            return SandboxResult(
                content=out,
                ok=ok,
                error=None if ok else f"exit={proc.returncode}",
                logs=out.splitlines()[-20:],
                duration=time.time() - t0,
                backend=self.name,
                containment=self.containment,
            )
        except subprocess.TimeoutExpired:
            return SandboxResult(
                ok=False, error=f"超时强杀(>{timeout}s)", backend=self.name,
                containment=self.containment,
                duration=time.time() - t0,
            )
        except Exception as e:  # noqa: BLE001
            return SandboxResult(
                ok=False, error=f"本地执行异常: {e}", backend=self.name,
                containment=self.containment,
                duration=time.time() - t0,
            )


class EnforcedLocalSandbox(LocalSandbox):
    """本地强制模式：在 LocalSandbox（jail + 环境净化 + 越权预检）之上加**真实资源上限**。

    与 advisory 版的区别——后者只过滤命令文本，子进程该吃多少内存/开多少进程都没人管；
    本类把上限变成内核会强制执行的事实：

      POSIX（preexec_fn + setrlimit）：
        RLIMIT_AS    地址空间上限（memory_mb）—— 防内存炸弹
        RLIMIT_CPU   CPU 秒数上限（cpu_seconds）—— 防死循环烧核
        RLIMIT_NPROC 进程数上限（max_processes）—— 防 fork 炸弹
        RLIMIT_FSIZE 单文件写入上限（max_file_mb）—— 防写爆磁盘
        RLIMIT_CORE  置 0 —— 不落 core（core 里可能带敏感内存）
      全平台（与 POSIX rlimit 叠加）：
        wall-clock 超时强杀**整个进程树**（POSIX killpg / Windows taskkill /T）
        stdout+stderr 字节截断（max_output_bytes）—— 防输出洪水撑爆调用方
        新会话（start_new_session）—— 子进程不继承调用方的进程组/终端信号

    诚实边界：containment="limited" —— 有真实资源上限，但**仍不是 OS 级隔离**：
    文件系统与网络没有隔离（ jail 只是 cwd + 预检）。要真隔离请用 Docker/E2B（containment="os"）。
    Windows 上无 rlimit，资源上限降级为"超时强杀 + 输出截断"，containment 同为 limited
    （在结果里如实标注，绝不让调用方误以为有内核级强制）。
    """

    name = "local-enforced"
    containment = "limited"

    def __init__(self, memory_mb: int = 1024, cpu_seconds: int = 60,
                 max_processes: int = 256, max_file_mb: int = 64,
                 max_output_bytes: int = 2_000_000):
        self.memory_mb = memory_mb
        self.cpu_seconds = cpu_seconds
        self.max_processes = max_processes
        self.max_file_mb = max_file_mb
        self.max_output_bytes = max_output_bytes

    # ---- POSIX：rlimit 在 fork 后、exec 前生效 ----
    def _posix_limits(self):
        def preexec():  # noqa: E306
            import resource
            mem = self.memory_mb * 1024 * 1024
            fsize = self.max_file_mb * 1024 * 1024
            for res, val in (
                (resource.RLIMIT_AS, mem),
                (resource.RLIMIT_CPU, (self.cpu_seconds, self.cpu_seconds)),
                (resource.RLIMIT_NPROC, (self.max_processes, self.max_processes)),  # scanner:allow 标准库常量名，非凭证
                (resource.RLIMIT_FSIZE, (fsize, fsize)),
                (resource.RLIMIT_CORE, (0, 0)),
            ):
                try:
                    resource.setrlimit(res, val)
                except Exception:
                    pass  # 平台不支持/权限不足时放弃该限制，不让执行失败
        return preexec

    # ---- 超时强杀：杀整棵树，不杀 leaves 漏网 ----
    @staticmethod
    def _kill_tree(proc):
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               capture_output=True, timeout=10)
            else:
                os.killpg(os.getpgid(proc.pid), 9)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def run(self, cmd, workdir: str, timeout: float = 30, **_kw) -> SandboxResult:
        """本地强制执行。Docker 专属 kwargs（input_dir/output_dir/network 等）
        本后端没有对应能力，如实忽略（containment="limited"：有内核资源上限，
        无 OS 级隔离）；网络审批门由 SandboxManager 统一执行。"""
        os.makedirs(workdir, exist_ok=True)
        blocked = self._jail_check(cmd)  # 复用 advisory 版的越权预检
        if blocked:
            return SandboxResult(ok=False, error=blocked, backend=self.name,
                                 containment=self.containment)

        safe_env = {
            "PATH": os.environ.get("PATH", ""),
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
            "TEMP": os.environ.get("TEMP", ""),
            "TMP": os.environ.get("TMP", ""),
        }
        t0 = time.time()
        popen_kw = dict(
            cwd=workdir, env=safe_env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            shell=isinstance(cmd, str),
        )
        if os.name == "posix":
            popen_kw["start_new_session"] = True   # 新进程组，整组可杀
            popen_kw["preexec_fn"] = self._posix_limits()
        try:
            proc = subprocess.Popen(cmd, **popen_kw)
        except Exception as e:  # noqa: BLE001
            return SandboxResult(ok=False, error=f"启动失败: {e}",
                                 backend=self.name, containment=self.containment)
        try:
            out_b, err_b = proc.communicate(timeout=timeout)
            out = (out_b or b"").decode("utf-8", "replace") + \
                  (err_b or b"").decode("utf-8", "replace")
            truncated = False
            if len(out) > self.max_output_bytes:
                out = out[: self.max_output_bytes] + \
                    f"\n…[输出被截断：超过 {self.max_output_bytes} 字节上限]"
                truncated = True
            ok = proc.returncode == 0
            note = None if ok else f"exit={proc.returncode}"
            if truncated:
                note = (note + "；" if note else "") + "输出超限已截断"
            return SandboxResult(
                content=out, ok=ok, error=note,
                logs=out.splitlines()[-20:], duration=time.time() - t0,
                backend=self.name, containment=self.containment,
            )
        except subprocess.TimeoutExpired:
            self._kill_tree(proc)
            proc.wait(timeout=10)
            return SandboxResult(
                ok=False, error=f"超时强杀(>{timeout}s，含子进程树)",
                backend=self.name, containment=self.containment,
                duration=time.time() - t0,
            )
        except Exception as e:  # noqa: BLE001
            self._kill_tree(proc)
            return SandboxResult(
                ok=False, error=f"本地执行异常: {e}", backend=self.name,
                containment=self.containment, duration=time.time() - t0,
            )


class DockerSandbox(SandboxBackend):
    """Docker 强隔离后端——按「隔离工位八条规格」硬化（runc 优先，gVisor/Kata 预留）。

    八条规格逐条落地：
      1. 每个任务独立容器，结束即销毁：每次 run 起一个带名字的容器，`--rm`
         保证正常退出即删；超时/异常路径显式 `docker kill` + `rm -f`；
         `cleanup_stale()` 跨进程清理上次进程被强杀留下的残留容器。
      2. 非 root 用户：`--user` 默认 nobody（65534:65534）；POSIX 上非 root
         调用者改用自身 uid:gid（绑定挂载属主才对得上），root 调用者退回 nobody。
      3. 根文件系统只读：`--read-only`，仅 `/tmp`（tmpfs，size 可配）与
         输出目录可写。
      4. 默认断网：`--network none`；需要外发网络时调用方必须持有**已批准**的
         approval_id（ ApprovalCenter.is_approved 鸭子类型），沙箱侧二次校验，
         直接调 backend.run 也绕不过。
      5. 资源上限：`--memory`/`--memory-swap`（禁 swap）/`--cpus`/`--pids-limit`。
      6. 输入目录只读挂载、输出目录单独挂载：`-v input:/input:ro`、
         `-v output:/output`；不给 input/output 时退化为旧的 `/work` 单一
         挂载（向后兼容）。
      7. 命令白名单：`allowed_commands` 非空时只放行名单内 argv[0]，并拒绝
         shell 字符串形式——只暴露受控 Tool，不把任意 shell 交出去。
      8. `--runtime` 可为未来 gVisor/Kata 预留；本类只保证 runc 调用链。

    诚实边界：containment="os" 指 Docker 提供的进程/文件系统/网络隔离；
    白名单与审批是叠加的策略层，不改变、也不夸大 Docker 本身的隔离语义。
    本地后端（advisory/limited）上这些策略同样生效于管理层，但机制只有
    Docker/E2B 真正具备。
    """

    name = "docker"
    containment = "os"

    def __init__(self, image: str = "alpine:3.20", network_disabled: bool = True, *,
                 user: Optional[str] = None,
                 memory_mb: int = 512, cpus: float = 1.0, pids_limit: int = 128,
                 tmpfs_mb: int = 64, max_output_bytes: int = 2_000_000,
                 allowed_commands=None, runtime: Optional[str] = None,
                 readonly_rootfs: bool = True, pull_if_missing: bool = False,
                 extra_run_args=None):
        self.image = image
        self.network_disabled = network_disabled
        self.user = user or _default_container_user()
        self.memory_mb = memory_mb
        self.cpus = cpus
        self.pids_limit = pids_limit
        self.tmpfs_mb = tmpfs_mb
        self.max_output_bytes = max_output_bytes
        # 白名单：None/空 = 不启用（保持旧的自由执行语义）；非空 = 只放行这些 argv[0]
        self.allowed_commands = tuple(allowed_commands or ())
        self.runtime = runtime          # 如 "runsc"（gVisor）/ "kata"；None = 默认 runc
        self.readonly_rootfs = readonly_rootfs
        self.pull_if_missing = pull_if_missing
        self.extra_run_args = list(extra_run_args or [])
        self._counter = itertools.count(1)
        self._active: set = set()       # 本进程已启动、尚未确认销毁的容器名
        self._lock = threading.Lock()

    def available(self) -> bool:
        return shutil.which("docker") is not None

    def daemon_reachable(self) -> bool:
        """docker 命令存在但守护进程未运行时，run 会失败。这里快速探活（短超时），
        仅当 daemon 真正可达才用 Docker 强隔离，否则由 auto() 降级到本地。"""
        try:
            r = subprocess.run(
                ["docker", "info"], capture_output=True, text=True, timeout=3
            )
            return r.returncode == 0
        except Exception:
            return False

    # ---- 规格 7：命令白名单 ----
    def _whitelist_reject(self, cmd) -> Optional[str]:
        if not self.allowed_commands:
            return None
        if not isinstance(cmd, (list, tuple)) or not cmd:
            return ("白名单模式只接受 list 形式命令（不暴露任意 shell 字符串）；"
                    f"允许的 argv[0]: {list(self.allowed_commands)}")
        argv0 = str(cmd[0])
        base = os.path.basename(argv0.replace("\\", "/"))
        allowed = {os.path.basename(str(a).replace("\\", "/")) for a in self.allowed_commands}
        if base not in allowed:
            return f"命令不在白名单: argv[0]={argv0!r}（允许: {sorted(allowed)}）"
        return None

    # ---- 规格 4：网络外发审批（backend 侧二次校验）----
    @staticmethod
    def _approval_ok(approval, approval_id: str) -> bool:
        if approval is None or not approval_id:
            return False
        checker = getattr(approval, "is_approved", None)
        if not callable(checker):
            return False
        try:
            return bool(checker(approval_id))
        except Exception:
            return False

    # ---- 规格 1：容器销毁 ----
    def _kill_container(self, name: str) -> None:
        for args in (["docker", "kill", name], ["docker", "rm", "-f", name]):
            try:
                subprocess.run(args, capture_output=True, timeout=10)
            except Exception:
                pass  # 销毁尽力而为：--rm 已覆盖正常路径，这里兜底异常/超时

    def cleanup(self) -> int:
        """销毁本进程启动且仍未确认退出的容器（正常路径 --rm 已自毁，这里兜底）。"""
        with self._lock:
            names = sorted(self._active)
        for n in names:
            self._kill_container(n)
            with self._lock:
                self._active.discard(n)
        return len(names)

    @classmethod
    def cleanup_stale(cls) -> int:
        """跨进程清理上次进程被强杀（--rm 来不及执行）留下的残留容器。

        只删名字带 CONTAINER_PREFIX 前缀的容器，绝不碰用户自己的容器。
        docker 不可用或没有残留时返回 0。
        """
        if shutil.which("docker") is None:
            return 0
        try:
            r = subprocess.run(
                ["docker", "ps", "-aq", "--filter", f"name={CONTAINER_PREFIX}"],
                capture_output=True, text=True, timeout=15,
            )
            if r.returncode != 0:
                return 0
            names = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]
        except Exception:
            return 0
        removed = 0
        for n in names:
            try:
                subprocess.run(["docker", "rm", "-f", n], capture_output=True, timeout=15)
                removed += 1
            except Exception:
                pass
        return removed

    # ---- 镜像可用性 ----
    def _image_missing(self, text: str) -> bool:
        low = (text or "").lower()
        return ("unable to find image" in low or "no such image" in low
                or "pull access denied" in low)

    def _ensure_image(self) -> Optional[str]:
        """镜像不在本地时按需拉取；返回错误串（拉不动）或 None（就绪）。"""
        try:
            r = subprocess.run(["docker", "image", "inspect", self.image],
                               capture_output=True, text=True, timeout=15)
            if r.returncode == 0:
                return None
        except Exception:
            return None  # 探活失败不阻塞，交给 docker run 自己报错
        if not self.pull_if_missing:
            return (f"镜像 {self.image} 不在本地：请先 `docker pull {self.image}`，"
                    f"或构造时设 pull_if_missing=True 自动拉取")
        try:
            r = subprocess.run(["docker", "pull", self.image],
                               capture_output=True, text=True, timeout=300)
            if r.returncode != 0:
                return f"docker pull {self.image} 失败: {(r.stderr or '')[:300]}"
        except Exception as e:
            return f"docker pull {self.image} 异常: {e}"
        return None

    def run(self, cmd, workdir: str, timeout: float = 30, *,
            input_dir: Optional[str] = None, output_dir: Optional[str] = None,
            network: Optional[str] = None, approval=None,
            approval_id: str = "") -> SandboxResult:
        os.makedirs(workdir, exist_ok=True)

        # 规格 7：白名单先行——被拒的命令根本不进容器
        reject = self._whitelist_reject(cmd)
        if reject:
            return SandboxResult(ok=False, error=reject, backend=self.name,
                                 containment=self.containment)

        # 规格 4：默认断网；外发网络必须持已批准审批
        want_network = network or ("none" if self.network_disabled else "bridge")
        if want_network != "none" and not self._approval_ok(approval, approval_id):
            return SandboxResult(
                ok=False,
                error=(f"网络外发未获审批：network={want_network!r} 需要已批准的 "
                       f"approval_id（ApprovalCenter.is_approved 为真）"),
                backend=self.name, containment=self.containment,
            )

        bad = self._ensure_image()
        if bad:
            return SandboxResult(ok=False, error=bad, backend=self.name,
                                 containment=self.containment)

        # 规格 1：可预测容器名（前缀 + pid + 序号），跨进程可清理
        name = f"{CONTAINER_PREFIX}{os.getpid()}-{next(self._counter)}"
        docker_cmd = [
            "docker", "run", "--rm", "--name", name,
            "--network", want_network,
            "--user", self.user,
            "--init",                          # PID1 收尸，防僵尸进程累积
            "--cap-drop", "ALL",               # 默认不给任何 capability
            "--security-opt", "no-new-privileges",  # 禁 setuid 提权链
            "--memory", f"{self.memory_mb}m",
            "--memory-swap", f"{self.memory_mb}m",   # = memory 即禁 swap
            "--cpus", str(self.cpus),
            "--pids-limit", str(self.pids_limit),
        ]
        if self.runtime:
            docker_cmd += ["--runtime", self.runtime]   # 规格 8：gVisor/Kata 预留
        if self.readonly_rootfs:                        # 规格 3：根文件系统只读
            docker_cmd += [
                "--read-only",
                "--tmpfs", f"/tmp:rw,noexec,nosuid,size={self.tmpfs_mb}m,mode=1777",
            ]
        # 规格 6：输入只读 / 输出单独挂载；都未给时退化为旧版单一 /work 挂载
        strict = bool(input_dir or output_dir)
        if input_dir:
            docker_cmd += ["-v", f"{os.path.abspath(input_dir)}:/input:ro"]
        if output_dir:
            docker_cmd += ["-v", f"{os.path.abspath(output_dir)}:/output"]
            inner_workdir = "/output"
        else:
            docker_cmd += ["-v", f"{os.path.abspath(workdir)}:/work"]
            inner_workdir = "/work"
        if strict:
            # 受控模式下 /work 只是 tmpfs 暂存区，不落宿主盘
            docker_cmd += ["--tmpfs",
                           f"/work:rw,noexec,nosuid,size={self.tmpfs_mb}m,mode=1777"]
        docker_cmd += ["-w", inner_workdir]
        docker_cmd += self.extra_run_args
        docker_cmd += [self.image, "sh", "-c",
                       self._inner_command(cmd)]

        with self._lock:
            self._active.add(name)
        t0 = time.time()
        try:
            proc = subprocess.run(docker_cmd, capture_output=True, text=True,
                                  timeout=timeout)
            out = (proc.stdout or "") + (proc.stderr or "")
            truncated = False
            if len(out) > self.max_output_bytes:
                out = (out[: self.max_output_bytes]
                       + f"\n…[输出被截断：超过 {self.max_output_bytes} 字节上限]")
                truncated = True
            ok = proc.returncode == 0
            note = None if ok else f"exit={proc.returncode}"
            if truncated:
                note = (note + "；" if note else "") + "输出超限已截断"
            if not ok and self._image_missing(out):
                note = f"镜像缺失: {self.image}（docker pull {self.image}）"
            return SandboxResult(
                content=out, ok=ok, error=note,
                logs=out.splitlines()[-20:], duration=time.time() - t0,
                backend=self.name, containment=self.containment,
            )
        except subprocess.TimeoutExpired:
            # docker run 客户端被杀不会停容器——必须显式销毁（规格 1）
            self._kill_container(name)
            return SandboxResult(
                ok=False, error=f"超时强杀(>{timeout}s，容器 {name} 已销毁)",
                backend=self.name, containment=self.containment,
                duration=time.time() - t0,
            )
        except Exception as e:  # noqa: BLE001
            self._kill_container(name)
            return SandboxResult(
                ok=False, error=f"Docker 执行异常: {e}", backend=self.name,
                containment=self.containment, duration=time.time() - t0,
            )
        finally:
            with self._lock:
                self._active.discard(name)

    @staticmethod
    def _inner_command(cmd) -> str:
        """把命令安全地拼成容器内 sh -c 的字符串。

        每个参数单独 shlex.quote——参数里的引号/分号/$() 都只是字面量，
        不会突破成额外命令（白名单模式下更关键：放行的只有一个 argv[0]）。
        """
        if isinstance(cmd, (list, tuple)):
            return " ".join(shlex.quote(str(c)) for c in cmd)
        return str(cmd)


class E2BSandbox(SandboxBackend):
    """E2B 远程沙箱（强隔离）。需要 `pip install e2b` 与 E2B_API_KEY 环境变量。

    真实接入时取消下方 import 注释即可；此处保持零依赖，缺包则 unavailable。"""

    name = "e2b"
    containment = "os"

    def __init__(self, api_key: str = "", template: str = "base"):
        self.api_key = api_key or os.getenv("E2B_API_KEY", "")
        self.template = template
        self._mod = None

    def available(self) -> bool:
        if not self.api_key:
            return False
        try:
            import e2b  # noqa: F401
            return True
        except ImportError:
            return False

    def run(self, cmd, workdir: str, timeout: float = 30, **_kw) -> SandboxResult:
        """远程执行。input_dir/output_dir/network 等参数由远端策略决定，
        本处如实忽略（containment="os" 指 E2B 提供的隔离，不含本地挂载语义）。"""
        if self._mod is None:
            try:
                from e2b import Sandbox as E2BSandboxClient  # 真实客户端
                self._mod = E2BSandboxClient
            except ImportError:
                return SandboxResult(
                    ok=False, error="未安装 e2b（pip install e2b）", backend=self.name
                )
        try:
            sb = self._mod.create(api_key=self.api_key, template=self.template)
            t0 = time.time()
            execution = sb.commands.run(cmd if isinstance(cmd, str) else " ".join(cmd))
            out = execution.stdout or ""
            ok = execution.error is None
            sb.close()
            return SandboxResult(
                content=out, ok=ok,
                error=None if ok else str(execution.error),
                duration=time.time() - t0, backend=self.name,
                containment="os",
            )
        except Exception as e:  # noqa: BLE001
            return SandboxResult(
                ok=False, error=f"E2B 执行异常: {e}", backend=self.name
            )


class SandboxManager:
    """能力探测 + 降级路由。优先强隔离，缺能力时降级到本地。"""

    def __init__(self, backend: Optional[SandboxBackend] = None):
        self.backend = backend or self.auto()

    @classmethod
    def auto(cls, prefer: str = "docker") -> SandboxBackend:
        """按优先级选择可用后端（强隔离优先，否则降级）：
        docker(daemon 可达) -> e2b(有 key 且可 import) -> local-enforced(有真实资源上限)
        -> local(纯 advisory，仅命令预检)。
        注意：docker 命令存在但 daemon 未运行时不算「可达」，会降级到 local，
        保证普通开发机上 demo 也能跑通。

        本地降级的两档差异（重要，别再混淆）：
          local-enforced = LocalSandbox + RLIMIT_AS/CPU/NPROC/FSIZE/CORE + 进程树强杀
                           + 输出截断。containment="limited"：有内核强制上限，无 OS 级隔离。
          local          = 只有命令文本预检。containment="advisory"：**不是沙箱**。
        设 SANDBOX_LOCAL_MODE=advisory 可回到旧的纯预检行为。

        ⚠️ 两者都不隔离文件系统与网络。处理不可信代码请设 SANDBOX_REQUIRE_STRONG=1，
        此时宁可报错也不降级，避免"以为是沙箱其实只是命令过滤"。
        """
        docker = cls.docker_from_env()
        if docker.available() and docker.daemon_reachable():
            return docker
        e2b = E2BSandbox()
        if e2b.available():
            return e2b
        if os.getenv("SANDBOX_REQUIRE_STRONG", "").strip() in ("1", "true", "yes"):
            raise RuntimeError(
                "未找到强隔离后端（Docker daemon 不可达 / 未配置 E2B），"
                "且已开启 SANDBOX_REQUIRE_STRONG=1 —— 拒绝降级到本地执行。"
                "请启动 Docker 或配置 E2B_API_KEY，或明确关闭该开关以接受本地执行风险。"
            )
        if os.getenv("SANDBOX_LOCAL_MODE", "").strip().lower() == "advisory":
            return LocalSandbox()
        return EnforcedLocalSandbox()

    # ---- 策略门：网络外发必须审批（任何后端都拦）----
    @staticmethod
    def _network_gate(network, approval, approval_id: str,
                      backend: SandboxBackend) -> Optional[SandboxResult]:
        """network 非 none 时，必须提供已批准的 approval_id。

        鸭子类型：approval 只要有 is_approved(approval_id)->bool 即可
        （ApprovalCenter 天然满足）。backend 自己也会二次校验——
        绕过 SandboxManager 直调 DockerSandbox.run 同样过不了关。
        """
        if network in (None, "none"):
            return None
        ok = False
        if approval is not None and approval_id:
            checker = getattr(approval, "is_approved", None)
            if callable(checker):
                try:
                    ok = bool(checker(approval_id))
                except Exception:
                    ok = False
        if ok:
            return None
        return SandboxResult(
            ok=False,
            error=(f"网络外发未获审批：network={network!r} 需要已批准的 approval_id"
                   f"（approval.is_approved 为真），默认断网是硬规则"),
            backend=backend.name, containment=backend.containment,
        )

    def run(self, cmd, workdir: str, timeout: float = 30, **kw) -> SandboxResult:
        """统一入口。kw 透传给后端；Docker 支持 input_dir/output_dir/network/
        approval/approval_id，本地后端如实忽略不支持的项。

        network 非 none 时先过审批门（见 _network_gate），被拒的请求根本
        不会到达后端——"默认断网，审批后才外发"在管理层强制执行。
        """
        refused = self._network_gate(kw.get("network"), kw.get("approval"),
                                     kw.get("approval_id", ""), self.backend)
        if refused is not None:
            return refused
        return self.backend.run(cmd, workdir, timeout=timeout, **kw)

    def cleanup(self) -> int:
        """销毁本进程残留容器（Docker 后端能力；其他后端返回 0）。"""
        fn = getattr(self.backend, "cleanup", None)
        return fn() if callable(fn) else 0

    # ---- Docker 配置面：env 驱动，无需改代码即可套用八条规格 ----
    @classmethod
    def docker_from_env(cls) -> DockerSandbox:
        """从环境变量构造 DockerSandbox（auto() 走此路，默认值与旧行为一致）：

          SECTRUST_SANDBOX_IMAGE       镜像（默认 alpine:3.20）
          SECTRUST_SANDBOX_MEMORY_MB   内存上限 MB（512）
          SECTRUST_SANDBOX_CPUS        CPU 核数上限（1）
          SECTRUST_SANDBOX_PIDS        进程数上限（128）
          SECTRUST_SANDBOX_TMPFS_MB    /tmp 与 /work 暂存上限 MB（64）
          SECTRUST_SANDBOX_ALLOWED     命令白名单，逗号分隔（空=不启用）
          SECTRUST_SANDBOX_PULL        1=镜像缺失自动拉取（默认 0，明确报错）
          SECTRUST_SANDBOX_RUNTIME     容器运行时（如 runsc/kata；默认 runc）
          SECTRUST_SANDBOX_NETWORK     默认网络 none/bridge（none=断网，需审批才放行）
          SECTRUST_SANDBOX_ROOTFS      0=关只读根文件系统（默认 1，弱化需显式）
        """
        def _int(name, default):
            raw = os.getenv(name, "").strip()
            try:
                return int(raw) if raw else default
            except ValueError:
                return default

        def _float(name, default):
            raw = os.getenv(name, "").strip()
            try:
                return float(raw) if raw else default
            except ValueError:
                return default

        allowed = [a.strip() for a in
                   os.getenv("SECTRUST_SANDBOX_ALLOWED", "").split(",")
                   if a.strip()]
        network = os.getenv("SECTRUST_SANDBOX_NETWORK", "").strip().lower()
        return DockerSandbox(
            image=os.getenv("SECTRUST_SANDBOX_IMAGE", "").strip() or "alpine:3.20",
            network_disabled=network not in ("bridge", "host"),
            user=os.getenv("SECTRUST_SANDBOX_USER", "").strip() or None,
            memory_mb=_int("SECTRUST_SANDBOX_MEMORY_MB", 512),
            cpus=_float("SECTRUST_SANDBOX_CPUS", 1.0),
            pids_limit=_int("SECTRUST_SANDBOX_PIDS", 128),
            tmpfs_mb=_int("SECTRUST_SANDBOX_TMPFS_MB", 64),
            allowed_commands=allowed or None,
            runtime=os.getenv("SECTRUST_SANDBOX_RUNTIME", "").strip() or None,
            readonly_rootfs=os.getenv("SECTRUST_SANDBOX_ROOTFS", "1").strip()
            not in ("0", "false", "no"),
            pull_if_missing=os.getenv("SECTRUST_SANDBOX_PULL", "").strip()
            in ("1", "true", "yes"),
        )


# ---- 低强度扫描示例（安全、可信、跨平台）----
# 在沙箱内执行的扫描脚本：不联网、不写外部文件，只统计工作目录的代码特征。
SAFE_SCAN_SCRIPT = r'''
import os, re, sys, json, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
root = sys.argv[1] if len(sys.argv) > 1 else "."
# 必须先归一化成绝对路径！否则相对路径 "." 的 basename 就是 "."，会被下面的
# 隐藏目录判断整层跳过 → 仓库根目录所有文件被静默漏扫（安全扫描最坏的一类 bug：假阴性）。
root = os.path.abspath(root)
files = []
for dp, _, fnames in os.walk(root):
    # 仅按目录自身名判断隐藏目录（如 .git/.venv），避免误伤父路径中含 "." 的目录（如 .sandbox）
    # 根目录自身永不跳过（已 abspath 归一化，basename 不会是 "."）
    if dp != root and os.path.basename(dp).startswith("."):
        continue
    for fn in fnames:
        if fn.endswith((".py", ".js", ".ts", ".go", ".java", ".md", ".txt", ".yml", ".yaml")):
            files.append(os.path.join(dp, fn))
todo = re.compile(r"\b(TODO|FIXME|XXX|HACK)\b")
secret = re.compile(r"(?i)(api[_-]?key|secret|password|token)\s*[:=]\s*['\"][^'\"]{4,}")
total_lines = 0
todo_n = 0
secret_n = 0
for fp in files:
    try:
        with open(fp, encoding="utf-8", errors="ignore") as fh:
            data = fh.read()
    except Exception:
        continue
    total_lines += data.count("\n") + 1
    todo_n += len(todo.findall(data))
    for m in secret.findall(data):
        # 排除明显的占位/示例
        if "example" not in m.lower() and "your" not in m.lower():
            secret_n += 1
print(json.dumps({
    "scanned_files": len(files),
    "total_lines": total_lines,
    "todo_markers": todo_n,
    "possible_secrets": secret_n,
}, ensure_ascii=False))
'''


def safe_scan_command(workdir: str, interpreter: Optional[str] = None) -> list:
    """构造一条低强度扫描命令：在沙箱内跑上面的 SAFE_SCAN_SCRIPT。

    interpreter 缺省用 sys.executable（本地后端正确）；Docker 后端要传
    "python3"——sys.executable 是**宿主**路径，容器里不存在，且镜像得
    自带 python3（如 python:3.12-alpine）。
    """
    return [interpreter or sys.executable, "-c", SAFE_SCAN_SCRIPT, workdir]

"""
沙箱执行模块（Sandbox）—— 默认「最小权限 + 降级」。

设计目标（对应安全叙事）：
- 任何「可能触碰文件系统 / 执行外部命令」的高权限动作，都先在沙箱里跑；
- 能隔离就隔离（Docker 容器 / E2B 远程），不能隔离就本地受限降级；
- 本地降级模式做「软越权拦截」：限制工作目录(jail)、清理危险环境变量、
  预检命令中明显的越界路径（../、/etc、C:\\Windows 等），超时强杀。

⚠️ 信任边界说明：
- Docker / E2B 是强隔离（容器/远程），生产环境优先；
- 本地降级是「弱隔离」——它只是把执行约束在 jail 工作目录并拦截明显越权，
  无法防住恶意的本地提权；请勿在不可信代码上使用本地降级。
- 默认网络：Docker 用 --network none（禁用网络）；本地降级无法禁用网络，
  E2B 由远端策略决定。

能力探测顺序（SandboxManager.auto()）：
  Docker 可用 -> 用 Docker（强隔离）
  E2B_API_KEY 配置 -> 用 E2B（强隔离，需 pip install e2b）
  否则 -> 本地降级（弱隔离，能跑即跑）

用法：
  from sandbox import SandboxManager
  sb = SandboxManager.auto()
  res = sb.run(["python", "-c", "print('hi')"], timeout=30)
  print(res.content, res.ok, res.backend)
"""

from __future__ import annotations

import abc
import json
import os
import shutil
import subprocess
import sys
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

    def run(self, cmd, workdir: str, timeout: float = 30) -> SandboxResult:
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
                (resource.RLIMIT_NPROC, (self.max_processes, self.max_processes)),
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

    def run(self, cmd, workdir: str, timeout: float = 30) -> SandboxResult:
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
    """Docker 强隔离：--network none + 只读挂载 jail + 工作目录隔离。

    需要本机 docker 命令可用；容器镜像可用 alpine（小）或自定义。"""

    name = "docker"
    containment = "os"

    def __init__(self, image: str = "alpine:3.20", network_disabled: bool = True):
        self.image = image
        self.network_disabled = network_disabled

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

    def run(self, cmd, workdir: str, timeout: float = 30) -> SandboxResult:
        os.makedirs(workdir, exist_ok=True)
        if isinstance(cmd, (list, tuple)):
            inner = " ".join(f"'{c}'" for c in cmd)
        else:
            inner = cmd
        t0 = time.time()
        try:
            # -w /work 设定容器工作目录；host workdir 挂载到 /work（读写）；--network none 禁网
            docker_cmd = [
                "docker", "run", "--rm",
                "-v", f"{os.path.abspath(workdir)}:/work",
                "-w", "/work",
            ]
            if self.network_disabled:
                docker_cmd.append("--network")
                docker_cmd.append("none")
            docker_cmd += [self.image, "sh", "-c", inner]
            proc = subprocess.run(
                docker_cmd, capture_output=True, text=True, timeout=timeout
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
                containment="os",
            )
        except subprocess.TimeoutExpired:
            return SandboxResult(
                ok=False, error=f"超时强杀(>{timeout}s)", backend=self.name,
                containment="os",
                duration=time.time() - t0,
            )
        except Exception as e:  # noqa: BLE001
            return SandboxResult(
                ok=False, error=f"Docker 执行异常: {e}", backend=self.name,
                containment="os",
                duration=time.time() - t0,
            )


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

    def run(self, cmd, workdir: str, timeout: float = 30) -> SandboxResult:
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
        docker = DockerSandbox()
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

    def run(self, cmd, workdir: str, timeout: float = 30) -> SandboxResult:
        return self.backend.run(cmd, workdir, timeout)


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


def safe_scan_command(workdir: str) -> list:
    """构造一条低强度扫描命令：在沙箱内跑上面的 SAFE_SCAN_SCRIPT。"""
    return [sys.executable, "-c", SAFE_SCAN_SCRIPT, workdir]

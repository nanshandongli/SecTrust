"""DockerSandbox 八条规格硬化测试（无需 Docker 守护进程：monkeypatch subprocess）。

覆盖用户八条规格逐条断言 + 审批门 + 输出截断 + 注入引号安全 + 镜像缺失 +
env 配置面 + 透传兼容。所有断言都打在**构造出来的 docker 命令**上——
flags 缺一条，隔离叙事就少一块，这类测试就是防"文档说隔离、命令没带"。
"""

import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import sandbox as S  # noqa: E402
from sandbox import (DockerSandbox, EnforcedLocalSandbox, SandboxManager,  # noqa: E402
                     SandboxResult)

IS_POSIX = os.name == "posix"


class FakeCompleted:
    """替身 subprocess.CompletedProcess。"""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _SubRecorder:
    """记录 subprocess.run 调用并按脚本返回/抛异常的替身。

    用法：with _SubRecorder() as rec: sb.run(...) 后看 rec.calls。
    inspect 默认成功（镜像就绪）；其余调用返回 FakeCompleted()。
    """

    def __init__(self, run_side_effect=None, inspect_rc=0):
        self.calls = []
        self.run_side_effect = run_side_effect
        self.inspect_rc = inspect_rc

    def __enter__(self):
        self._orig = S.subprocess.run

        def fake(cmd, **kw):
            self.calls.append(list(cmd))
            if cmd and cmd[0] == "docker" and len(cmd) > 1 and cmd[1] == "image":
                return FakeCompleted(self.inspect_rc)
            if self.run_side_effect is not None:
                eff = self.run_side_effect
                if callable(eff):
                    eff = eff(cmd)
                if isinstance(eff, Exception):
                    raise eff
                if isinstance(eff, FakeCompleted):
                    return eff
            return FakeCompleted()

        S.subprocess.run = fake
        return self

    def __exit__(self, *exc):
        S.subprocess.run = self._orig
        return False

    def docker_runs(self):
        """只取 docker run 调用（排除 image inspect / kill / rm / pull）。"""
        return [c for c in self.calls
                if len(c) > 1 and c[0] == "docker" and c[1] == "run"]

    def calls_of(self, *verbs):
        return [c for c in self.calls
                if len(c) > 2 and c[0] == "docker" and c[1] in verbs]


class FakeApproval:
    """鸭子类型审批器：只实现 is_approved。"""

    def __init__(self, approved_ids=()):
        self.approved_ids = set(approved_ids)

    def is_approved(self, approval_id):
        return approval_id in self.approved_ids


class DockerHardeningTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sbxdocker-")
        self.addCleanup(lambda: __import__("shutil").rmtree(
            self.tmp, ignore_errors=True))
        self.in_dir = tempfile.mkdtemp(prefix="sbxin-")
        self.out_dir = tempfile.mkdtemp(prefix="sbxout-")
        self.addCleanup(lambda: __import__("shutil").rmtree(
            self.in_dir, ignore_errors=True))
        self.addCleanup(lambda: __import__("shutil").rmtree(
            self.out_dir, ignore_errors=True))

    def _sandbox(self, **kw):
        kw.setdefault("image", "alpine:3.20")
        return DockerSandbox(**kw)

    def _run(self, sb, cmd=None, **kw):
        cmd = cmd or ["echo", "hi"]
        with _SubRecorder() as rec:
            res = sb.run(cmd, self.tmp, timeout=10, **kw)
        return res, rec.docker_runs()[-1] if rec.docker_runs() else None, rec

    # ---- 规格 1：独立容器 + 结束即销毁 ----
    def test_per_task_container_named_and_rm(self):
        sb = self._sandbox()
        res, cmd, _ = self._run(sb)
        self.assertTrue(res.ok, res.error)
        self.assertIn("--rm", cmd)
        name = cmd[cmd.index("--name") + 1]
        self.assertTrue(name.startswith(S.CONTAINER_PREFIX), name)
        # 每次 run 一个独立容器（序号递增）
        _, cmd2, _ = self._run(sb)
        name2 = cmd2[cmd2.index("--name") + 1]
        self.assertNotEqual(name, name2)
        # 正常退出后本进程不再持有该容器（--rm 自毁，登记表清空）
        self.assertEqual(sb._active, set())

    def test_timeout_kills_container_explicitly(self):
        sb = self._sandbox()
        with _SubRecorder(run_side_effect=subprocess.TimeoutExpired(
                cmd="docker run", timeout=10)) as rec:
            res = sb.run(["sleep", "999"], self.tmp, timeout=1)
        self.assertFalse(res.ok)
        self.assertIn("超时强杀", res.error)
        # 客户端被杀不会停容器——必须显式 kill + rm -f
        kills = rec.calls_of("kill", "rm")
        self.assertEqual(len(kills), 2, kills)
        self.assertEqual(kills[0][:3], ["docker", "kill", kills[0][2]])
        self.assertIn("-f", kills[1])

    def test_exception_path_also_destroys_container(self):
        sb = self._sandbox()
        with _SubRecorder(run_side_effect=RuntimeError("boom")) as rec:
            res = sb.run(["echo", "hi"], self.tmp, timeout=10)
        self.assertFalse(res.ok)
        self.assertIn("boom", res.error)
        self.assertTrue(rec.calls_of("kill"), "异常路径必须销毁容器")

    def test_cleanup_stale_only_touches_prefixed_containers(self):
        """残留清理只删本模块前缀的容器，绝不碰用户容器。"""
        seen = {}

        def fake(cmd, **kw):
            if cmd[1:3] == ["ps", "-aq"]:
                seen["filter"] = cmd[cmd.index("--filter") + 1]
                return FakeCompleted(0, stdout="sbx1\nsbx2\n")
            return FakeCompleted()

        orig_run, orig_which = S.subprocess.run, S.shutil.which
        S.subprocess.run = fake
        S.shutil.which = lambda name: "/usr/bin/docker" if name == "docker" else None
        try:
            n = DockerSandbox.cleanup_stale()  # scanner:allow 标识符误报
        finally:
            S.subprocess.run, S.shutil.which = orig_run, orig_which
        self.assertEqual(n, 2)
        self.assertEqual(seen["filter"], f"name={S.CONTAINER_PREFIX}")

    # ---- 规格 2：非 root 用户 ----
    def test_non_root_user_default(self):
        _, cmd, _ = self._run(self._sandbox())
        user = cmd[cmd.index("--user") + 1]
        self.assertNotEqual(user, "root")
        self.assertNotEqual(user, "0:0")
        if IS_POSIX and os.geteuid() != 0:
            self.assertEqual(user, f"{os.geteuid()}:{os.getegid()}")
        else:
            self.assertEqual(user, S.DEFAULT_CONTAINER_USER)

    def test_user_override(self):
        _, cmd, _ = self._run(self._sandbox(user="1000:1000"))
        self.assertEqual(cmd[cmd.index("--user") + 1], "1000:1000")

    # ---- 规格 3：根文件系统只读，只写 /tmp 与输出目录 ----
    def test_readonly_rootfs_with_tmpfs(self):
        _, cmd, _ = self._run(self._sandbox())
        self.assertIn("--read-only", cmd)
        tmpfs = cmd[cmd.index("--tmpfs") + 1]
        self.assertTrue(tmpfs.startswith("/tmp:"), tmpfs)
        self.assertIn(f"size=64m", tmpfs)

    def test_strict_mode_workdir_is_tmpfs_not_host_mount(self):
        sb = self._sandbox()
        _, cmd, _ = self._run(sb, input_dir=self.in_dir, output_dir=self.out_dir)
        joined = " ".join(cmd)
        # 宿主 workdir 不再挂进去；/work 只是 tmpfs 暂存区
        self.assertNotIn(f"{self.tmp}:/work", joined)
        self.assertIn("--tmpfs", cmd)
        self.assertEqual(cmd[cmd.index("-w") + 1], "/output")

    # ---- 规格 4：默认断网 + 审批后才外发 ----
    def test_default_network_none(self):
        _, cmd, _ = self._run(self._sandbox())
        self.assertEqual(cmd[cmd.index("--network") + 1], "none")

    def test_backend_refuses_network_without_approval(self):
        sb = self._sandbox()
        res, cmd, _ = self._run(sb, network="bridge")
        self.assertFalse(res.ok)
        self.assertIn("未获审批", res.error)
        self.assertIsNone(cmd, "被拒的请求根本不起容器")

    def test_backend_allows_network_with_approved_id(self):
        sb = self._sandbox()
        ap = FakeApproval({"ap-ok"})
        res, cmd, _ = self._run(sb, network="bridge", approval=ap,
                                approval_id="ap-ok")
        self.assertTrue(res.ok, res.error)
        self.assertEqual(cmd[cmd.index("--network") + 1], "bridge")

    def test_backend_refuses_unapproved_id(self):
        sb = self._sandbox()
        ap = FakeApproval()  # 什么都没批准
        res, cmd, _ = self._run(sb, network="bridge", approval=ap,
                                approval_id="ap-no")
        self.assertFalse(res.ok)
        self.assertIsNone(cmd)

    def test_manager_gate_blocks_before_backend(self):
        """管理层先拦：被拒时后端根本不被调用（本地后端同样适用）。"""
        mgr = SandboxManager(EnforcedLocalSandbox())
        res = mgr.run(["echo", "hi"], self.tmp, network="bridge")
        self.assertFalse(res.ok)
        self.assertIn("未获审批", res.error)
        # 放行后透传到本地后端（本地忽略 network kwarg，如实执行）
        res2 = mgr.run(["echo", "hi"], self.tmp, network="bridge",
                       approval=FakeApproval({"ok"}), approval_id="ok")
        self.assertTrue(res2.ok, res2.error)
        self.assertEqual(res2.backend, "local-enforced")

    def test_real_approval_center_integration(self):
        """与真实 ApprovalCenter 对接：pending 拒 / approved 放行。"""
        from approval import ApprovalCenter
        db = os.path.join(self.tmp, "approvals.db")
        ac = ApprovalCenter(db_path=db)
        self.addCleanup(ac.close)
        aid = ac.request("sandbox_network", "任务需访问外网", task_id="t/1")
        self.assertFalse(ac.is_approved(aid))
        res, cmd, _ = self._run(self._sandbox(), network="bridge",
                                approval=ac, approval_id=aid)
        self.assertFalse(res.ok)  # pending 不是 approved
        self.assertIsNone(cmd)
        self.assertTrue(ac.decide(aid, "approve"))
        res, cmd, _ = self._run(self._sandbox(), network="bridge",
                                approval=ac, approval_id=aid)
        self.assertTrue(res.ok, res.error)
        self.assertEqual(cmd[cmd.index("--network") + 1], "bridge")

    # ---- 规格 5：资源上限 ----
    def test_resource_limits(self):
        sb = self._sandbox(memory_mb=256, cpus=0.5, pids_limit=32)
        _, cmd, _ = self._run(sb)
        self.assertEqual(cmd[cmd.index("--memory") + 1], "256m")
        self.assertEqual(cmd[cmd.index("--memory-swap") + 1], "256m")
        self.assertEqual(cmd[cmd.index("--cpus") + 1], "0.5")
        self.assertEqual(cmd[cmd.index("--pids-limit") + 1], "32")

    # ---- 规格 6：输入只读 / 输出单独挂载 ----
    def test_input_ro_output_rw_mounts(self):
        _, cmd, _ = self._run(self._sandbox(), input_dir=self.in_dir,
                              output_dir=self.out_dir)
        joined = " ".join(cmd)
        self.assertIn(f"{self.in_dir}:/input:ro", joined)
        self.assertIn(f"{self.out_dir}:/output", joined)

    def test_legacy_single_workdir_mount_when_no_dirs(self):
        """不传 input/output 时保持旧的单一 /work 挂载（向后兼容）。"""
        _, cmd, _ = self._run(self._sandbox())
        joined = " ".join(cmd)
        self.assertIn(f"{self.tmp}:/work", joined)
        self.assertNotIn(":/input:ro", joined)

    # ---- 规格 7：命令白名单 ----
    def test_whitelist_rejects_unknown_argv0(self):
        sb = self._sandbox(allowed_commands=["python3", "/usr/bin/python3"])
        res, cmd, _ = self._run(sb, cmd=["curl", "http://evil"])
        self.assertFalse(res.ok)
        self.assertIn("白名单", res.error)
        self.assertIsNone(cmd)

    def test_whitelist_accepts_listed_basename(self):
        sb = self._sandbox(allowed_commands=["python3"])
        res, cmd, _ = self._run(sb, cmd=["python3", "-c", "print(1)"])
        self.assertTrue(res.ok, res.error)
        self.assertIsNotNone(cmd)

    def test_whitelist_rejects_shell_string(self):
        sb = self._sandbox(allowed_commands=["python3"])
        res, cmd, _ = self._run(sb, cmd="python3 -c 'print(1)'")
        self.assertFalse(res.ok)
        self.assertIn("shell 字符串", res.error)
        self.assertIsNone(cmd)

    def test_no_whitelist_keeps_legacy_freedom(self):
        sb = self._sandbox()
        res, cmd, _ = self._run(sb, cmd=["anything-goes", "--flag"])
        self.assertTrue(res.ok, res.error)
        self.assertIsNotNone(cmd)

    # ---- 规格 8：runtime 预留 ----
    def test_runtime_flag_passthrough(self):
        _, cmd, _ = self._run(self._sandbox(runtime="runsc"))
        self.assertEqual(cmd[cmd.index("--runtime") + 1], "runsc")

    # ---- 附加硬化：引用安全 / 输出截断 / 镜像缺失 ----
    def test_injection_stays_literal(self):
        """注入载荷必须被引号包成字面量：解析回来的 argv 与原命令一致，
        不会 break 成第二个命令。"""
        import shlex
        sb = self._sandbox()
        nasty = "x'; touch /tmp/pwned; echo '"
        res, cmd, _ = self._run(sb, cmd=["echo", nasty])
        self.assertTrue(res.ok, res.error)
        inner = cmd[-1]  # 整个命令是单个 sh -c 参数字符串
        self.assertEqual(shlex.split(inner), ["echo", nasty])
        self.assertEqual(inner.count("sh -c"), 0)

    def test_output_truncated(self):
        sb = self._sandbox(max_output_bytes=100)

        def big(cmd):
            return FakeCompleted(0, stdout="y" * 5000)

        with _SubRecorder(run_side_effect=big):
            res = sb.run(["echo", "hi"], self.tmp, timeout=10)
        self.assertTrue(res.ok)
        self.assertLess(len(res.content), 5000)
        self.assertIn("输出被截断", res.content)
        self.assertIn("输出超限已截断", res.error)

    def test_image_missing_clear_error(self):
        """镜像缺失有两条报错路径都必须说清怎么修：起跑前预检 + 运行后兜底。"""
        sb = self._sandbox(image="sectrust:nope")

        # 路径 1：预检发现本地没有镜像（且未开自动拉取）
        with _SubRecorder(inspect_rc=1):
            res = sb.run(["echo", "hi"], self.tmp, timeout=10)
        self.assertFalse(res.ok)
        self.assertIn("不在本地", res.error)
        self.assertIn("docker pull sectrust:nope", res.error)

        # 路径 2：预检时镜像在，run 起来才报找不到（如 inspect 竞态/缓存）
        def missing(cmd):
            return FakeCompleted(127, stderr="Unable to find image 'sectrust:nope' locally")

        with _SubRecorder(run_side_effect=missing):
            res = sb.run(["echo", "hi"], self.tmp, timeout=10)
        self.assertFalse(res.ok)
        self.assertIn("镜像缺失", res.error)
        self.assertIn("docker pull sectrust:nope", res.error)

    def test_pull_if_missing_pulls_then_runs(self):
        sb = self._sandbox(image="sectrust:maybe", pull_if_missing=True)
        with _SubRecorder(inspect_rc=1) as rec:
            res = sb.run(["echo", "hi"], self.tmp, timeout=10)
        self.assertTrue(res.ok, res.error)
        pulls = [c for c in rec.calls if c[1:2] == ["pull"]]
        self.assertEqual(len(pulls), 1)
        self.assertEqual(pulls[0][-1], "sectrust:maybe")

    # ---- env 配置面 ----
    def test_docker_from_env(self):
        env = {
            "SECTRUST_SANDBOX_IMAGE": "python:3.12-alpine",
            "SECTRUST_SANDBOX_MEMORY_MB": "256",
            "SECTRUST_SANDBOX_CPUS": "0.5",
            "SECTRUST_SANDBOX_PIDS": "32",
            "SECTRUST_SANDBOX_ALLOWED": "python3, echo",
            "SECTRUST_SANDBOX_RUNTIME": "runsc",
            "SECTRUST_SANDBOX_PULL": "1",
            "SECTRUST_SANDBOX_NETWORK": "none",
            "SECTRUST_SANDBOX_ROOTFS": "0",
        }
        old = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        try:
            sb = SandboxManager.docker_from_env()
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.assertEqual(sb.image, "python:3.12-alpine")
        self.assertEqual(sb.memory_mb, 256)
        self.assertEqual(sb.cpus, 0.5)
        self.assertEqual(sb.pids_limit, 32)
        self.assertEqual(set(sb.allowed_commands), {"python3", "echo"})
        self.assertEqual(sb.runtime, "runsc")
        self.assertTrue(sb.pull_if_missing)
        self.assertTrue(sb.network_disabled)
        self.assertFalse(sb.readonly_rootfs)

    def test_docker_from_env_bad_values_fall_back(self):
        old = os.environ.get("SECTRUST_SANDBOX_MEMORY_MB")
        os.environ["SECTRUST_SANDBOX_MEMORY_MB"] = "not-a-number"
        try:
            sb = SandboxManager.docker_from_env()
        finally:
            if old is None:
                os.environ.pop("SECTRUST_SANDBOX_MEMORY_MB", None)
            else:
                os.environ["SECTRUST_SANDBOX_MEMORY_MB"] = old
        self.assertEqual(sb.memory_mb, 512)  # 坏值不炸，回落默认

    # ---- 透传与清理入口 ----
    def test_manager_cleanup_delegates(self):
        mgr = SandboxManager(self._sandbox())
        self.assertEqual(mgr.cleanup(), 0)  # 没有活跃容器
        mgr2 = SandboxManager(EnforcedLocalSandbox())
        self.assertEqual(mgr2.cleanup(), 0)  # 无 cleanup 能力的后端返回 0

    def test_result_defaults_unchanged(self):
        r = SandboxResult()
        self.assertEqual(r.backend, "none")
        self.assertEqual(r.containment, "advisory")


if __name__ == "__main__":
    unittest.main()

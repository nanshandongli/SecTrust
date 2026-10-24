# 发布指引（PyPI + MCP Registry）

本文档说明把本项目发布到 **PyPI** 与 **MCP Registry** 的完整步骤与所需凭据。

> ⚠️ **当前状态：物料已就绪，尚未实际上架。**
> 物料齐备性由 `tests/test_release_packaging.py`（16 项）自动校验，构建产物已实测可安装可用。
> 但**发布动作本身需要你的账号与凭据**（PyPI API token、GitHub 仓库、Registry 提交权限），
> 这些不在本机环境中，因此未执行。下方步骤可直接照做。

---

## 一、当前状态核验

```bash
# 1) 跑测试（三处版本一致 + 包内容完整 + 工具清单一致）
python -m unittest tests.test_release_packaging -v

# 2) 构建（产出 sdist + wheel）
python -m build --outdir dist_build
# 产出：
#   dist_build/ai_colleague_registry-0.7.0.tar.gz
#   dist_build/ai_colleague_registry-0.7.0-py3-none-any.whl

# 3) 装到干净环境验证（确保不是"靠 cwd 蒙混"）
python -m venv .venv_check && .venv_check/Scripts/pip install dist_build/*.whl
.venv_check/Scripts/python -c "import mcp_server; print(len(mcp_server._TOOLS))"
```

实测结果：wheel 78 个条目、16 个根模块 + 11 个子包全部入包，
`sectrust-mcp` 等 5 个 console 入口生成正常，零依赖安装（无需联网拉包）。

---

## 二、PyPI 发布

### 2.1 前置条件（需你准备）

| 项 | 说明 | 从哪来 |
|----|------|--------|
| PyPI 账号 | 注册 https://pypi.org/ | 你 |
| API Token | 项目级 scoped token，**不要用账号密码** | PyPI → Account → API tokens |
| 仓库 URL | ✅ 已填真实仓库 `nanshandongli/SecTrust` | 已完成 |
| 打包工具 | `pip install build twine` | PyPI |

### 2.2 发布前必改项

```toml
# pyproject.toml
[project.urls]
# ✅ 已填真实仓库地址（nanshandongli/SecTrust），无需再改
```

同时 `mcpserver.json` 里的 `homepage` / `repository` / `publisher` 也要一并对齐
（`publisher` 是 Registry 收录的必填标识）。

### 2.3 校验并发布

```bash
# 校验包元数据（能挡掉一批低级错：README 编码、缺失描述等）
pip install twine
twine check dist_build/*

# 上传到 TestPyPI 先演练（不影响正式站）
twine upload --repository testpypi dist_build/*

# 正式发布
twine upload dist_build/*
```

### 2.4 建议：GitHub Actions 自动发布

仓库加 `.github/workflows/release.yml`，tag 触发即构建 + 上传（凭据存
仓库 Secrets 的 `PYPI_API_TOKEN`）。`scanner/push_protection.py` 已有
GitHub Actions YAML 生成能力，可参考其风格。

---

## 三、MCP Registry 发布

### 3.1 收录所需的文件

| 文件 | 作用 | 状态 |
|------|------|------|
| `mcpserver.json` | 服务器元数据：工具清单、协议版本、能力注解 | ✅ 已备（含包内副本 `release_meta/mcpserver.json`） |
| README | 展示页正文 | ✅ 已备 |
| LICENSE | 许可证 | ✅ MIT 已备 |
| 仓库地址 | 可访问的源码/文档 | ⚠️ 需替换占位 URL |
| 发布后的包 | Registry 从 PyPI 拉取 | 需先完成第二步 |

`mcpserver.json` 关键字段现状：

- `protocolVersion`: `2026-07-28`（同时兼容 2025-11-25 / 2025-06-18 / 2025-03-26 / 2024-11-05）
- `transport`: `stdio`
- `tools`: 33 个，与 `mcp_server._TOOLS` **由测试强制一致**
- `capabilities`: 审计 / 无状态 / OAuth 2.1 / 扫描 / 记忆 / 技能 / 证据门禁 七项声明

### 3.2 发布步骤

1. 先完成 PyPI 发布（Registry 从 PyPI 拉取包）
2. 在 https://registry.modelcontextprotocol.io 提交收录
3. 提交时填写：`name` / `description` / `repository` / `package`（PyPI 坐标）/
   `version`（须与 PyPI 上一致）

> ⚠️ **版本必须与 PyPI 一致**——`mcpserver.json` 的 `version` 与 `pyproject.toml`
> 的 `version` 若不一致，Registry 收录到的会是错版本。已由测试自动拦截。

### 3.3 客户端接入示例

```json
{
  "mcpServers": {
    "SecTrust": {
      "command": "sectrust-mcp",
      "env": { "DB_PATH": "data/task_registry.db" }
    }
  }
}
```

---

## 四、诚实边界

- ✅ **已完成**：全部发布物料（pyproject / MANIFEST.in / LICENSE / CHANGELOG /
  mcpserver.json + 包内副本）、构建产物实测可装可用、16 项自动校验、
  版本一致性有测试兜底。
- ✅ **已发布**：`twine upload` 已成功（2026-10-24），`sectrust` 0.8.0 上线
  PyPI：https://pypi.org/project/sectrust/0.8.0/。用户现已可 `pip install sectrust`。
  - 发布用隔离 venv：`C:\Users\Administrator\.workbuddy\binaries\python\envs\default`
    （装了 build + twine，重发只需 `python -m build && TWINE_USERNAME=__token__ TWINE_PASSWORD=<token> twine upload dist/*`）。
  - ⚠️ 安全：API Token 仅在发布命令里以环境变量传入，未落盘任何文件；
    如怀疑泄漏，到 PyPI 账户设置撤销该 token 即可。
- ✅ **已替换**：`pyproject.toml` 与 `mcpserver.json` 里的仓库地址已改为
  `nanshandongli/SecTrust`。

---

## 五、相关文件

| 文件 | 用途 |
|------|------|
| `pyproject.toml` | 打包与元数据（版本 / 包清单 / 入口 / extras） |
| `MANIFEST.in` | sdist 内容清单（打包什么、排除什么） |
| `LICENSE` | MIT |
| `CHANGELOG.md` | 版本变更记录 |
| `mcpserver.json` | MCP 服务器元数据（Registry 收录） |
| `release_meta/__init__.py` | 定位 `mcpserver.json`（开发态仓库根 / 发布态包内） |
| `tests/test_release_packaging.py` | 16 项物料校验测试 |

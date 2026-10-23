#!/usr/bin/env bash
#
# 一键安装脚本（Linux / macOS / Git Bash / WSL）
#
# 职责：
#   1. 检查 Python >= 3.10 与 SQLite（零依赖，标准库 sqlite3 即可）
#   2. 创建运行时目录 data/ 与 .sandbox/
#   3. 探测沙箱后端：Docker 可用 -> 强隔离；E2B_API_KEY 配置 -> 远程强隔离；
#      否则 -> 本地受限降级（会明确提示这是弱隔离）
#   4. 从 .env.example 生成 .env（已存在则跳过，不覆盖）
#   5. 打印下一步启动命令
#
# 用法：
#   bash install.sh
# 或赋予执行权限后：
#   ./install.sh

set -euo pipefail

echo "=============================================="
echo " 跨进程任务登记处 + 安全 AI 同事平台 · 安装"
echo "=============================================="

# ---------- 1. 检查 Python ----------
PY_BIN=""
for cand in python3 python; do
  if command -v "$cand" >/dev/null 2>&1; then
    if "$cand" -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" 2>/dev/null; then
      PY_BIN="$cand"
      break
    fi
  fi
done

if [ -z "$PY_BIN" ]; then
  echo "[错误] 需要 Python >= 3.10，但未找到或版本过低。"
  echo "        请先安装：https://www.python.org/downloads/"
  exit 1
fi
PY_VER=$("$PY_BIN" -c "import sys; print('.'.join(map(str, sys.version_info[:3])))")
echo "[OK] Python: $PY_BIN ($PY_VER)"

# ---------- 2. 检查 SQLite ----------
if "$PY_BIN" -c "import sqlite3; print('sqlite3', sqlite3.sqlite_version)" >/dev/null 2>&1; then
  SQL_VER=$("$PY_BIN" -c "import sqlite3; print(sqlite3.sqlite_version)")
  echo "[OK] SQLite: $SQL_VER（标准库，无需额外安装）"
else
  echo "[错误] Python 缺少 sqlite3 模块。"
  exit 1
fi

# ---------- 3. 创建目录 ----------
mkdir -p data .sandbox
echo "[OK] 已创建/复用目录: data/ .sandbox/"

# ---------- 4. 探测沙箱后端（降级策略）----------
echo "---- 沙箱后端探测 ----"
SANDBOX_BACKEND="local(降级)"
if command -v docker >/dev/null 2>&1; then
  if docker info >/dev/null 2>&1; then
    SANDBOX_BACKEND="docker(强隔离)"
    echo "[OK] 检测到 Docker 守护进程可用 -> 沙箱将使用 Docker 强隔离（--network none）。"
  else
    echo "[提示] 检测到 docker 命令，但守护进程未运行。可启动 Docker 后用强隔离；"
    echo "        否则回退本地受限降级（弱隔离，请勿跑不可信代码）。"
  fi
fi
if [ -n "${E2B_API_KEY:-}" ]; then
  echo "[OK] 检测到 E2B_API_KEY -> 可启用 E2B 远程沙箱（需 pip install e2b）。"
  if "$PY_BIN" -c "import e2b" >/dev/null 2>&1; then
    SANDBOX_BACKEND="e2b(远程强隔离)"
  else
    echo "        [提示] e2b 包未安装，运行: pip install e2b"
  fi
fi
if [ "$SANDBOX_BACKEND" = "local(降级)" ]; then
  echo "[注意] 未检测到 Docker / E2B -> 沙箱使用『本地受限降级』（弱隔离）。"
  echo "        生产环境强烈建议配置 Docker 或 E2B 以获得容器级隔离。"
fi

# ---------- 5. 生成示例配置 ----------
if [ -f .env.example ]; then
  if [ -f .env ]; then
    echo "[跳过] .env 已存在，未覆盖（如需重置请手动删除 .env）。"
  else
    cp .env.example .env
    echo "[OK] 已从 .env.example 生成 .env（请按需填写密钥；默认全为空=纯离线 Mock 模式）。"
  fi
else
  echo "[提示] 未找到 .env.example，跳过配置生成。"
fi

# ---------- 6. 运行冒烟（确认依赖齐备）----------
echo "---- 冒烟检查 ----"
"$PY_BIN" -c "import task_registry, approval, sandbox, channels, models, skills; print('[OK] 核心模块导入成功')" || {
  echo "[错误] 核心模块导入失败，请检查当前目录是否为项目根。";
  exit 1;
}

# ---------- 7. 下一步 ----------
echo ""
echo "=============================================="
echo " 安装完成 ✅  沙箱后端: $SANDBOX_BACKEND"
echo "=============================================="
echo " 下一步（任选）："
echo ""
echo " 1) 看 live_view（只读仪表盘）："
echo "      $PY_BIN task_center_web.py --db-path data/task_registry.db --port 8090"
echo "      浏览器打开 http://127.0.0.1:8090/"
echo ""
echo " 2) 跑 3 分钟可复现 Demo（/scan repo 全流程）："
echo "      $PY_BIN examples/scan_repo_demo.py --auto-approve"
echo ""
echo " 3) 启动常驻运行时（mock 渠道 + mock 模型，离线）："
echo "      $PY_BIN runner.py --demo"
echo ""
echo " 4) 真人审批入口（不开 --auto-approve 时用）："
echo "      $PY_BIN approval.py --list"
echo "      $PY_BIN approval.py --approve <approval_id>"
echo ""
echo " 详细文档见 README.md / DESIGN.md / SECURITY.md"
echo "=============================================="

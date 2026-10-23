<#
.SYNOPSIS
    一键安装脚本（Windows PowerShell）
.DESCRIPTION
    1. 检查 Python >= 3.10 与 SQLite（标准库）
    2. 创建 data/ 与 .sandbox/
    3. 探测沙箱后端：Docker / E2B -> 强隔离；否则本地受限降级
    4. 从 .env.example 生成 .env（已存在则跳过）
    5. 冒烟检查核心模块导入
    6. 打印下一步命令
.USAGE
    powershell -ExecutionPolicy Bypass -File install.ps1
#>

$ErrorActionPreference = "Stop"
Write-Host "==============================================" -ForegroundColor Cyan
Write-Host " 跨进程任务登记处 + 安全 AI 同事平台 · 安装" -ForegroundColor Cyan
Write-Host "=============================================="

# ---------- 1. 检查 Python ----------
$pyBin = $null
foreach ($cand in @("python", "python3")) {
    if (Get-Command $cand -ErrorAction SilentlyContinue) {
        try {
            $ver = & $cand -c "import sys; print('.'.join(map(str, sys.version_info[:3])))" 2>$null
            $major = [int]($ver.Split('.')[0]); $minor = [int]($ver.Split('.')[1])
            if ($major -ge 3 -and $minor -ge 10) { $pyBin = $cand; break }
        } catch {}
    }
}
if (-not $pyBin) {
    Write-Host "[错误] 需要 Python >= 3.10，但未找到或版本过低。" -ForegroundColor Red
    Write-Host "        请先安装：https://www.python.org/downloads/" -ForegroundColor Red
    exit 1
}
$pyVer = & $pyBin -c "import sys; print('.'.join(map(str, sys.version_info[:3])))"
Write-Host "[OK] Python: $pyBin ($pyVer)" -ForegroundColor Green

# ---------- 2. 检查 SQLite ----------
try {
    $sqlVer = & $pyBin -c "import sqlite3; print(sqlite3.sqlite_version)" 2>$null
    Write-Host "[OK] SQLite: $sqlVer (标准库，无需额外安装)" -ForegroundColor Green
} catch {
    Write-Host "[错误] Python 缺少 sqlite3 模块。" -ForegroundColor Red
    exit 1
}

# ---------- 3. 创建目录 ----------
New-Item -ItemType Directory -Force -Path data | Out-Null
New-Item -ItemType Directory -Force -Path .sandbox | Out-Null
Write-Host "[OK] 已创建/复用目录: data/ .sandbox/" -ForegroundColor Green

# ---------- 4. 探测沙箱后端（降级策略）----------
Write-Host "---- 沙箱后端探测 ----"
$sandboxBackend = "local(降级)"
if (Get-Command docker -ErrorAction SilentlyContinue) {
    try {
        $null = & docker info 2>$null
        if ($LASTEXITCODE -eq 0) {
            $sandboxBackend = "docker(强隔离)"
            Write-Host "[OK] 检测到 Docker 守护进程可用 -> 沙箱将使用 Docker 强隔离（--network none）。" -ForegroundColor Green
        } else {
            Write-Host "[提示] 检测到 docker 命令，但守护进程未运行。可启动 Docker 后用强隔离；否则回退本地受限降级（弱隔离）。" -ForegroundColor Yellow
        }
    } catch {}
}
$e2bKey = [Environment]::GetEnvironmentVariable("E2B_API_KEY")
if ($e2bKey) {
    Write-Host "[OK] 检测到 E2B_API_KEY -> 可启用 E2B 远程沙箱（需 pip install e2b）。" -ForegroundColor Green
    try {
        & $pyBin -c "import e2b" 2>$null
        if ($LASTEXITCODE -eq 0) { $sandboxBackend = "e2b(远程强隔离)" }
        else { Write-Host "        [提示] e2b 包未安装，运行: pip install e2b" -ForegroundColor Yellow }
    } catch {}
}
if ($sandboxBackend -eq "local(降级)") {
    Write-Host "[注意] 未检测到 Docker / E2B -> 沙箱使用『本地受限降级』（弱隔离）。" -ForegroundColor Yellow
    Write-Host "        生产环境强烈建议配置 Docker 或 E2B 以获得容器级隔离。" -ForegroundColor Yellow
}

# ---------- 5. 生成示例配置 ----------
if (Test-Path .env.example) {
    if (Test-Path .env) {
        Write-Host "[跳过] .env 已存在，未覆盖（如需重置请手动删除 .env）。" -ForegroundColor Yellow
    } else {
        Copy-Item .env.example .env
        Write-Host "[OK] 已从 .env.example 生成 .env（请按需填写密钥；默认全为空=纯离线 Mock 模式）。" -ForegroundColor Green
    }
} else {
    Write-Host "[提示] 未找到 .env.example，跳过配置生成。" -ForegroundColor Yellow
}

# ---------- 6. 冒烟检查 ----------
Write-Host "---- 冒烟检查 ----"
& $pyBin -c "import task_registry, approval, sandbox, channels, models, skills; print('[OK] 核心模块导入成功')"
if ($LASTEXITCODE -ne 0) {
    Write-Host "[错误] 核心模块导入失败，请检查当前目录是否为项目根。" -ForegroundColor Red
    exit 1
}

# ---------- 7. 下一步 ----------
Write-Host ""
Write-Host "==============================================" -ForegroundColor Cyan
Write-Host " 安装完成 ✅  沙箱后端: $sandboxBackend" -ForegroundColor Cyan
Write-Host "==============================================" -ForegroundColor Cyan
Write-Host " 下一步（任选）："
Write-Host ""
Write-Host " 1) 看 live_view（只读仪表盘）："
Write-Host "      $pyBin task_center_web.py --db-path data/task_registry.db --port 8090"
Write-Host "      浏览器打开 http://127.0.0.1:8090/"
Write-Host ""
Write-Host " 2) 跑 3 分钟可复现 Demo（/scan repo 全流程）："
Write-Host "      $pyBin examples/scan_repo_demo.py --auto-approve"
Write-Host ""
Write-Host " 3) 启动常驻运行时（mock 渠道 + mock 模型，离线）："
Write-Host "      $pyBin runner.py --demo"
Write-Host ""
Write-Host " 4) 真人审批入口（不开 --auto-approve 时用）："
Write-Host "      $pyBin approval.py --list"
Write-Host "      $pyBin approval.py --approve <approval_id>"
Write-Host ""
Write-Host " 详细文档见 README.md / DESIGN.md / SECURITY.md"
Write-Host "==============================================" -ForegroundColor Cyan

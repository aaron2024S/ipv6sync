# 一键离线组装 + 校验 ipv6sync 镜像 tar
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File build.ps1              # 全流程（默认只出 x64）
#   powershell -ExecutionPolicy Bypass -File build.ps1 -SkipFetch   # 跳过拉基础层（用缓存）
#   powershell -ExecutionPolicy Bypass -File build.ps1 -Only all    # 需要 ARM 时显式指定
#
# 产物：dist/  （整个目录拷到 NAS 即可，不需要源码、不需要外网）
param(
    [switch]$SkipFetch,
    [switch]$SkipBootCheck,
    [ValidateSet("all", "amd64", "arm64")]
    [string]$Only = "amd64"
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

$env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)

# 找 python：优先本机托管运行时，退化到 PATH 上的 python
$candidates = @(
    "C:\Users\$env:USERNAME\.workbuddy\binaries\python\versions\3.13.12\python.exe",
    "C:\Users\$env:USERNAME\.workbuddy\binaries\python\versions\3.13.11\python.exe"
)
$py = $null
foreach ($c in $candidates) { if (Test-Path $c) { $py = $c; break } }
if (-not $py) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { $py = $cmd.Source }
}
if (-not $py) { Write-Host "找不到 python，请手动指定" -ForegroundColor Red; exit 1 }
Write-Host "python: $py" -ForegroundColor Cyan

# 可复现构建：固定时间戳，同样输入产出逐字节相同的 tar
if (-not $env:SOURCE_DATE_EPOCH) {
    $env:SOURCE_DATE_EPOCH = (& $py -c "import datetime;print(int(datetime.datetime(2026,9,21,0,0,0,tzinfo=datetime.timezone.utc).timestamp()))").Trim()
}
Write-Host "SOURCE_DATE_EPOCH=$env:SOURCE_DATE_EPOCH" -ForegroundColor Cyan

function Step($n, $msg) {
    Write-Host ""
    Write-Host ("=" * 60) -ForegroundColor DarkGray
    Write-Host "[$n] $msg" -ForegroundColor Yellow
}
function Run-Py($script, $argList) {
    & $py (Join-Path $here $script) @argList
    if ($LASTEXITCODE -ne 0) {
        Write-Host "`n$script 失败（exit=$LASTEXITCODE）" -ForegroundColor Red
        exit $LASTEXITCODE
    }
}

# 1. 拉基础层
if (-not $SkipFetch) {
    Step 1 "拉取基础镜像层到 cache/"
    Run-Py "fetch_base.py" @("--arch", $Only)
} else {
    Step 1 "跳过拉取（-SkipFetch）"
}

# 2. 组装
Step 2 "组装镜像 tar 到 dist/"
Run-Py "build_offline.py" @("--arch", $Only, "--report", (Join-Path $here "dist\build-report.json"))

# 3. 静态校验
Step 3 "静态校验（层哈希 / 路径卫生 / 入口 ELF / 属主）"
Run-Py "validate_image.py" @("--arch", $Only)

# 4. 行为校验（应用层与架构无关，两个架构各跑一次）
if (-not $SkipBootCheck) {
    Step 4 "行为校验（复现工作目录、跑单测、验 healthcheck）"
    $arches = if ($Only -eq "all") { @("amd64", "arm64") } else { @($Only) }
    foreach ($a in $arches) { Run-Py "validate_boot.py" @("--arch", $a) }
} else {
    Step 4 "跳过行为校验（-SkipBootCheck）"
}

# 5. 把部署物料一并放进 dist/，让交付目录自成一套
Step 5 "打包部署物料到 dist/"
$dist = Join-Path $here "dist"
Copy-Item (Join-Path $here "docker-compose.offline.yml") $dist -Force
Copy-Item (Join-Path $here "..\.env.example") $dist -Force
Copy-Item (Join-Path $here "README.md") $dist -Force
Get-ChildItem $dist | Select-Object Name, @{n="MB"; e={[math]::Round($_.Length / 1e6, 2)}} |
    Format-Table -AutoSize | Out-String | Write-Host

Write-Host ""
Write-Host "完成。交付目录： $dist" -ForegroundColor Green
Write-Host "把它拷到 NAS 后按 README.md 的「在 NAS 上部署」操作即可。" -ForegroundColor Green

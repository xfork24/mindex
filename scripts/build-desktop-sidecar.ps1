# 构建 Windows 桌面版冻结后端（PyInstaller onedir）：desktop/build/backend/mindex.exe。
# scripts/build-desktop-sidecar.sh 的 Windows 原生等价流程（同一 desktop/sidecar/mindex.spec）：
# 在独立虚拟环境中按 uv.lock 安装依赖（主依赖 + animation、mcp extra；不含 dev、person），
# 以普通 wheel 安装 mindex，再用锁定版本的 PyInstaller 打包。不改动仓库的 .venv。
$ErrorActionPreference = "Stop"

$Root = (Resolve-Path "$PSScriptRoot\..").Path
$Build = Join-Path $Root "desktop\build"
$Venv = Join-Path $Build "sidecar-venv"
$VenvPython = Join-Path $Venv "Scripts\python.exe"
$PythonVersion = if ($env:MINDEX_SIDECAR_PYTHON) { $env:MINDEX_SIDECAR_PYTHON } else { "3.13" }
$PyInstallerVersion = "6.22.3"

if ($env:OS -ne "Windows_NT") {
    throw "本脚本只在 Windows 上运行；macOS/Linux 请用 scripts/build-desktop-sidecar.sh"
}
foreach ($tool in @("uv")) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        throw "缺少构建工具：$tool"
    }
}

New-Item -ItemType Directory -Force -Path $Build | Out-Null
foreach ($stale in @($Venv, (Join-Path $Build "backend"), (Join-Path $Build "pyinstaller-work"))) {
    if (Test-Path $stale) { Remove-Item -Recurse -Force $stale }
}
# ErrorActionPreference=Stop 对原生命令的非零退出无效，每条之后显式检查。
uv venv --quiet --python $PythonVersion $Venv
if ($LASTEXITCODE -ne 0) { throw "uv venv 退出码 $LASTEXITCODE" }
uv export --quiet --project $Root --frozen --no-dev --extra animation --extra mcp `
    --no-emit-project --no-hashes --output-file (Join-Path $Build "sidecar-requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "uv export 退出码 $LASTEXITCODE" }
uv pip install --quiet --python $VenvPython -r (Join-Path $Build "sidecar-requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "uv pip install（依赖）退出码 $LASTEXITCODE" }
# 非 editable 安装：package-data（前端、模板）进入 site-packages，PyInstaller 从这里收集。
uv pip install --quiet --python $VenvPython --no-deps --reinstall $Root
if ($LASTEXITCODE -ne 0) { throw "uv pip install（mindex）退出码 $LASTEXITCODE" }
uv pip install --quiet --python $VenvPython "pyinstaller==$PyInstallerVersion"
if ($LASTEXITCODE -ne 0) { throw "uv pip install（pyinstaller）退出码 $LASTEXITCODE" }

Push-Location (Join-Path $Root "desktop\sidecar")
try {
    & (Join-Path $Venv "Scripts\pyinstaller.exe") --noconfirm --clean --log-level WARN `
        --distpath $Build --workpath (Join-Path $Build "pyinstaller-work") mindex.spec
    if ($LASTEXITCODE -ne 0) { throw "pyinstaller 退出码 $LASTEXITCODE" }
} finally {
    Pop-Location
}
Remove-Item -Recurse -Force (Join-Path $Build "pyinstaller-work")
$backend = Join-Path $Build "backend\mindex.exe"
$size = "{0:N1} MB" -f ((Get-ChildItem -Recurse (Join-Path $Build "backend") | Measure-Object Length -Sum).Sum / 1MB)
Write-Host "冻结后端：$backend（$size）"

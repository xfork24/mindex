# 构建 Windows 桌面版安装包（x64）：desktop/dist/Mindex-<版本>-x64.{exe[NSIS],zip}，
# 以及 GPL 对应源码包 desktop/dist/mindex-desktop-ffmpeg-sources-<版本>.zip。
#
#   powershell scripts/build-desktop-win.ps1 [-RebuildFfmpeg] [-SkipSidecar]
#
# 步骤与 macOS 版一致：随包 ffmpeg（缺失或 -RebuildFfmpeg 时经 MSYS2/UCRT64 从源码编译）→
# 冻结后端 → 冻结后端冒烟 → 字体 → electron-builder（NSIS + 便携 zip）。Windows 不签名
# （SmartScreen 提示见 docs/desktop.md）。需要：MSYS2（UCRT64 工具链，见 build-desktop-ffmpeg.sh
# 的平台说明，可用 MINDEX_MSYS2 指定安装目录，默认 C:\msys64）、uv、node 22+。
param(
    [switch]$RebuildFfmpeg,
    [switch]$SkipSidecar
)
$ErrorActionPreference = "Stop"

$Root = (Resolve-Path "$PSScriptRoot\..").Path
$Desktop = Join-Path $Root "desktop"
$Build = Join-Path $Desktop "build"
$Msys2 = if ($env:MINDEX_MSYS2) { $env:MINDEX_MSYS2 } else { "C:\msys64" }
$MsysShell = Join-Path $Msys2 "msys2_shell.cmd"

if ($env:OS -ne "Windows_NT") {
    throw "本脚本只在 Windows 上运行；macOS 用 scripts/build-desktop-mac.sh，Linux 用 scripts/build-desktop-linux.sh"
}
foreach ($tool in @("uv", "node", "npm")) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        throw "缺少构建工具：$tool"
    }
}

# 版本一致性：desktop/package.json 与 pyproject.toml。
$pyproject = Get-Content (Join-Path $Root "pyproject.toml") -Raw
if ($pyproject -notmatch '(?m)^version\s*=\s*"([^"]+)"') { throw "无法从 pyproject.toml 读取版本" }
$version = $Matches[1]
$package = Get-Content (Join-Path $Desktop "package.json") -Raw | ConvertFrom-Json
if ($package.version -ne $version) {
    throw "desktop/package.json 版本 $($package.version) 与 pyproject.toml $version 不一致"
}

# Noto Sans CJK SC（SIL OFL 1.1）：烧录字幕的随包字体（与 macOS 版同一份）。
$fontTag = "Sans2.004"
$fontSha256 = "2c76254f6fc379fddfce0a7e84fb5385bb135d3e399294f6eeb6680d0365b74b"
$licenseSha256 = "6a73f9541c2de74158c0e7cf6b0a58ef774f5a780bf191f2d7ec9cc53efe2bf2"
function Fetch-File {
    param([string]$Url, [string]$Target, [string]$Sha256)
    $needs = -not (Test-Path $Target)
    if (-not $needs) {
        $needs = ((Get-FileHash $Target -Algorithm SHA256).Hash -ne $Sha256)
    }
    if ($needs) {
        Invoke-WebRequest -Uri $Url -OutFile "$Target.partial"
        Move-Item "$Target.partial" $Target -Force
    }
    if ((Get-FileHash $Target -Algorithm SHA256).Hash -ne $Sha256) {
        throw "sha256 不符：$Target"
    }
}
New-Item -ItemType Directory -Force -Path (Join-Path $Build "fonts") | Out-Null
Fetch-File "https://raw.githubusercontent.com/notofonts/noto-cjk/$fontTag/Sans/OTF/SimplifiedChinese/NotoSansCJKsc-Regular.otf" `
    (Join-Path $Build "fonts\NotoSansCJKsc-Regular.otf") $fontSha256
Fetch-File "https://raw.githubusercontent.com/notofonts/noto-cjk/main/Sans/LICENSE" `
    (Join-Path $Build "fonts\LICENSE-NotoSansCJK.txt") $licenseSha256

# 随包 ffmpeg：经 MSYS2 UCRT64 跑共享的 build-desktop-ffmpeg.sh（sha256 锁定表与 mac/Linux 同源）。
$ffmpegBin = Join-Path $Build "ffmpeg\bin\ffmpeg.exe"
if ($RebuildFfmpeg -or -not (Test-Path $ffmpegBin)) {
    if (-not (Test-Path $MsysShell)) {
        throw "找不到 MSYS2：$MsysShell（用 MINDEX_MSYS2 指定安装目录）"
    }
    Push-Location $Root
    try {
        # -here：以当前目录（仓库根）为工作目录；脚本内部按自身路径定位 ROOT。
        & $MsysShell -ucrt64 -defterm -no-start -here -use-full-path -c "bash scripts/build-desktop-ffmpeg.sh"
        if ($LASTEXITCODE -ne 0) { throw "build-desktop-ffmpeg.sh 退出码 $LASTEXITCODE" }
    } finally {
        Pop-Location
    }
}

# 冻结后端。
$backendExe = Join-Path $Build "backend\mindex.exe"
if (-not $SkipSidecar -or -not (Test-Path $backendExe)) {
    & (Join-Path $PSScriptRoot "build-desktop-sidecar.ps1")
    if ($LASTEXITCODE -ne 0) { throw "build-desktop-sidecar.ps1 退出码 $LASTEXITCODE" }
}

# 冻结后端 + 随包 ffmpeg + 随包字体，在隔离 PATH 下冒烟（含中文字幕烧录）。
uv run --project $Root python (Join-Path $Root "scripts\check-desktop-sidecar.py") --isolated-path `
    --ffmpeg-dir (Join-Path $Build "ffmpeg\bin") --font (Join-Path $Build "fonts\NotoSansCJKsc-Regular.otf")
if ($LASTEXITCODE -ne 0) { throw "check-desktop-sidecar.py 退出码 $LASTEXITCODE" }

# electron-builder：NSIS 安装器 + 便携 zip。
Push-Location $Desktop
try {
    npm ci --no-audit --no-fund
    if ($LASTEXITCODE -ne 0) { throw "npm ci 退出码 $LASTEXITCODE" }
    if (Test-Path (Join-Path $Desktop "dist")) { Remove-Item -Recurse -Force (Join-Path $Desktop "dist") }
    npx electron-builder --win --x64 --publish never
    if ($LASTEXITCODE -ne 0) { throw "electron-builder 退出码 $LASTEXITCODE" }
} finally {
    Pop-Location
}

# GPL 对应源码包（zip；含共享 ffmpeg 脚本自拷贝与 Windows 构建脚本副本）。
$sources = Join-Path $Build "ffmpeg\sources"
Copy-Item "$PSScriptRoot\build-desktop-win.ps1", "$PSScriptRoot\build-desktop-sidecar.ps1" $sources -Force
Compress-Archive -Path "$sources\*" -DestinationPath (Join-Path $Desktop "dist\mindex-desktop-ffmpeg-sources-$version.zip") -Force
Write-Host "安装包："
Get-ChildItem (Join-Path $Desktop "dist\*") -Include *.exe, *.zip | Format-Table Name, Length

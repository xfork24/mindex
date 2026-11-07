# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 规格：冻结 mindex serve 为 onedir 后端（macOS arm64 / Linux x64 / Windows x64），
# 由 scripts/build-desktop-sidecar.sh（mac、Linux）或 build-desktop-sidecar.ps1（Windows）在独立环境中调用。
#
# 维护约定：
# - SOURCE_MODULES：以自身源码文件哈希作指纹的模块（content_hash(Path(__file__))）和需要以 .py 文件存在的脚本，
#   必须以源码收集，冻结后 __file__ 指向同一份 .py，哈希与源码运行完全相同。tests/test_desktop_frozen.py 会扫描检查。
#   llama_cpp.py 的哈希属于嵌入空间身份（adapter_sha256），不要为打包修改它。
# - 包数据（前端、文案模板）经 collect_data_files("mindex") 从已安装的 wheel 收集；新增 package-data 时
#   scripts/check-desktop-sidecar.py 会核对 pyproject 声明的文件都已打包。
# - console=True 三平台一致：stdout/stderr 管道依赖它；Windows 下黑框由外壳 spawn 的 windowsHide 消除。
# - target_arch/codesign_identity 仅 macOS 有意义，其他平台不传。
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules, copy_metadata
from deepseek_harness_runtime import resolve_bundled_launch_args

import mindex

# 要求当前目标平台的生产运行时完整；不使用系统 Node 或开发态 carrier。
resolve_bundled_launch_args("exe")

SOURCE_MODULES = {
    "models.llama_cpp": "py",
    "models.model_server": "py",
    "processing.align_worker": "py",
    "mindex.models.llama_cpp": "py",
    "mindex.models.model_server": "py",
    "mindex.processing.align_worker": "py",
}

datas, binaries, hiddenimports = [], [], []
hiddenimports += collect_submodules("mindex")
# 包内组件统一短名导入(shared.media 等,见 plans/code-style-normalization-plan.md 原始需求 2):
# 运行时 mindex/__init__ 把包目录加入 sys.path 后以短名加载,冻结分析需把短名模块一并收进 TOC。
# 从已安装的 wheel 收集短名模块及其数据，保证与 mindex.web 的资源属于同一次构建。
_BACKEND_ROOT = Path(mindex.__file__).resolve().parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))
for _short_pkg in (
    "admin",
    "agent",
    "api",
    "cli",
    "diagnostics",
    "maintenance",
    "models",
    "persons",
    "processing",
    "search",
    "server",
    "settings",
    "shared",
    "studio",
    "topics",
    "worker",
):
    hiddenimports += collect_submodules(_short_pkg)
    datas += collect_data_files(_short_pkg)
hiddenimports += collect_submodules("uvicorn")
# 只经 `mindex -m 模块` 运行、没有被任何模块导入的入口（AI 动画录制组件安装）。
hiddenimports += ["playwright.__main__"]
# 第三方 MCP 工具的客户端（studio/tools/mcp.py 惰性导入）。
hiddenimports += collect_submodules("mcp.client")
hiddenimports += collect_submodules("deepseek_harness")
hiddenimports += collect_submodules("deepseek_harness_runtime")
datas += collect_data_files("mindex")
datas += copy_metadata("mindex")
datas += collect_data_files("jieba")
# 命名时区在没有系统 IANA 数据的环境中依赖 tzdata；importlib 动态读取须显式收集。
hiddenimports += collect_submodules("tzdata")
datas += collect_data_files("tzdata")
for package in ("chromadb", "scenedetect"):
    package_datas, package_binaries, package_hidden = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden
# importlib.metadata 查询的依赖版本（uvicorn、fastapi 等启动时读取）。
for package in ("fastapi", "uvicorn", "pydantic", "chromadb", "httpx", "playwright", "mcp",
                "deepseek-harness-sdk", "deepseek-harness-runtime-bin", "tzdata"):
    datas += copy_metadata(package)

analysis = Analysis(
    ["launcher.py"],
    pathex=[str(_BACKEND_ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # 只在独立模型服务/对齐环境里使用的大依赖：桌面后端不需要。
    excludes=["torch", "transformers", "bitsandbytes", "tensorflow", "matplotlib", "tkinter", "IPython",
              "pytest", "hypothesis", "ruff", "insightface", "supervision", "av"],
    module_collection_mode=SOURCE_MODULES,
    noarchive=False,
)
# Node SEA 可执行文件必须原样复制，不能被二进制依赖分析、重写加载路径或 strip。
# onedir 的 COLLECT 会保留 DATA 的可执行位；macOS afterPack 再统一签名。
# 包含原生 runtime、rg、macOS spawn-helper、downloads.json 和技能资源，保持 wheel 目录布局。
analysis.datas += [(str(Path(destination) / Path(source).name), source, "DATA")
                   for source, destination in collect_data_files("deepseek_harness_runtime")]
pyz = PYZ(analysis.pure)
_darwin_only = {"target_arch": "arm64", "codesign_identity": None} if sys.platform == "darwin" else {}
executable = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="mindex",
    console=True,
    upx=False,
    **_darwin_only,
)
COLLECT(executable, analysis.binaries, analysis.datas, name="backend", upx=False)

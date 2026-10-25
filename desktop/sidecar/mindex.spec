# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 规格：冻结 mindex serve 为 onedir 后端（arm64），由 scripts/build-desktop-sidecar.sh 在独立环境中调用。
#
# 维护约定：
# - SOURCE_MODULES：以自身源码文件哈希作指纹的模块（content_hash(Path(__file__))）和需要以 .py 文件存在的脚本，
#   必须以源码收集，冻结后 __file__ 指向同一份 .py，哈希与源码运行完全相同。tests/test_desktop_frozen.py 会扫描检查。
#   llama_cpp.py 的哈希属于嵌入空间身份（adapter_sha256），不要为打包修改它。
# - 包数据（前端、文案模板）经 collect_data_files("mindex") 从已安装的 wheel 收集；新增 package-data 时
#   scripts/check-desktop-sidecar.py 会核对 pyproject 声明的文件都已打包。
from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules, copy_metadata

SOURCE_MODULES = {
    "mindex.models.llama_cpp": "py",
    "mindex.models.model_server": "py",
    "mindex.processing.align_worker": "py",
}

datas, binaries, hiddenimports = [], [], []
hiddenimports += collect_submodules("mindex")
hiddenimports += collect_submodules("uvicorn")
# 只经 `mindex -m 模块` 运行、没有被任何模块导入的入口（AI 动画录制组件安装）。
hiddenimports += ["playwright.__main__"]
datas += collect_data_files("mindex")
datas += copy_metadata("mindex")
datas += collect_data_files("jieba")
for package in ("chromadb", "scenedetect"):
    package_datas, package_binaries, package_hidden = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden
# importlib.metadata 查询的依赖版本（uvicorn、fastapi 等启动时读取）。
for package in ("fastapi", "uvicorn", "pydantic", "chromadb", "httpx", "playwright", "mcp"):
    datas += copy_metadata(package)

analysis = Analysis(
    ["launcher.py"],
    pathex=[],
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
pyz = PYZ(analysis.pure)
executable = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="mindex",
    console=True,
    target_arch="arm64",
    codesign_identity=None,
    upx=False,
)
COLLECT(executable, analysis.binaries, analysis.datas, name="backend", upx=False)

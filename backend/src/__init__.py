"""Local, versioned video retrieval."""

import sys
from pathlib import Path

__version__ = "0.1.0"

# 查询根目录修正:把本包目录加入 sys.path,包内组件可以短名导入
# (from shared.media import probe,而非 from mindex.shared.media import probe)。
# 全仓一律使用短名,禁止混用 mindex.* 子模块导入,避免同一文件被加载为两个模块身份。
_ROOT = str(Path(__file__).resolve().parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def main() -> None:
    """安装包入口在自举之后按短名装配 CLI，避免 cli 包被重复加载。"""
    # 延迟导入：安装入口先完成根包自举，再按短名装配以避免双模块身份。
    from cli import app

    app()


def person_server_main() -> None:
    """独立人物服务入口，与业务调用共用同一个 persons 模块。"""
    # 延迟导入：安装入口先完成根包自举，再按短名装配以避免双模块身份。
    from persons.person_server import main

    main()


def model_server_main() -> None:
    """独立模型服务入口，与业务调用共用同一个 models 模块。"""
    # 延迟导入：安装入口先完成根包自举，再按短名装配以避免双模块身份。
    from models.model_server import main

    main()

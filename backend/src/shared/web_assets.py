"""开发环境和安装包共用的前端资源定位。"""

from pathlib import Path


def frontend_root() -> Path:
    """wheel 内使用随包资源，源码运行时使用仓库 frontend/src。"""
    package_root = Path(__file__).resolve().parents[1]
    bundled = package_root / "web"
    if bundled.is_dir():
        return bundled
    source = package_root.parents[1] / "frontend" / "src"
    if source.is_dir():
        return source
    raise FileNotFoundError("找不到前端资源：请检查 frontend/src 或重新安装完整的 mindex 包")

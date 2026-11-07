"""桌面版冻结后端的入口（PyInstaller 可执行文件 ``mindex``）。

冻结后 ``sys.executable`` 是这个可执行文件本身。为了让现有的 ``[sys.executable, "-m", 模块, ...]`` 子进程
（备份向量迁移、人物 worker、``playwright install`` 等）无需修改即可工作：

- ``mindex -m <模块> [参数…]``：以 ``runpy`` 按 ``__main__`` 运行该模块；
- 其他参数：运行 ``mindex`` CLI（与源码安装的 ``mindex`` 命令相同）。

源码运行时不使用本入口。
"""

from __future__ import annotations

import os
import runpy
import sys

from cli import app
from settings.browser_install import browsers_path


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if getattr(sys, "frozen", False) and not os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        # playwright 在冻结环境里缺省把浏览器目录设为包内（PLAYWRIGHT_BROWSERS_PATH=0）；改为与源码运行相同的
        # 缺省缓存目录。桌面外壳总会显式设置为用户数据目录的 browsers/。

        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers_path())
    if len(args) >= 2 and args[0] == "-m":
        module = args[1]
        sys.argv = [module, *args[2:]]  # run_module(alter_sys=True) 会把 argv[0] 换成模块文件路径
        runpy.run_module(module, run_name="__main__", alter_sys=True)
        return
    if args[:1] == ["-m"]:
        sys.exit("用法：mindex -m <模块> [参数…]")

    sys.argv = [sys.argv[0], *args]
    app(prog_name="mindex")


if __name__ == "__main__":
    main()

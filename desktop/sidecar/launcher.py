"""PyInstaller 入口脚本：冻结后端可执行文件 ``mindex``。逻辑在 ``server.desktop_entry``。"""

import mindex  # noqa: F401 — 安装包自举，使包内组件可按短名加载
from server.desktop_entry import main

if __name__ == "__main__":
    main()

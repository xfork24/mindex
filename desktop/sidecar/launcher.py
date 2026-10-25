"""PyInstaller 入口脚本：冻结后端可执行文件 ``mindex``。逻辑在 ``mindex.desktop_entry``。"""

from mindex.desktop_entry import main

if __name__ == "__main__":
    main()

"""应用日志归属判断，与包内短导入使用相同的顶层包名。"""

APPLICATION_PACKAGES = frozenset(
    {
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
    }
)


def is_application_logger(name: str) -> bool:
    """判断短模块日志名是否属于应用，排除第三方库日志。"""
    return name.partition(".")[0] in APPLICATION_PACKAGES

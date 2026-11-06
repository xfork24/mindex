"""跨包共享的错误类型。

`ModelError` 原定义在 models/adapters；集群准入（shared/admission）也需要以同一类型
拒绝请求，而包边界禁止 shared 依赖 models，因此把类型下沉到这里，models 侧再导出，
全部既有导入路径与异常身份保持不变。
"""
from __future__ import annotations


class ModelError(RuntimeError):
    pass

"""分词器（从 search/search.py 下沉到 shared，供检索与派生词法索引共用）。

``tokens``/``document_terms`` 是词法检索的唯一分词实现：SQL 倒排（shared/
lexical_store.py）与 Python 参考路径必须复用同一实现，且分词器版本
（jieba 版本、词典规模、本实现的源码指纹）随派生索引持久化——版本不一致的
节点禁止构建/查询新派生索引（plans/horizontal-scaling-plan.md §6.1）。
"""

from __future__ import annotations

import hashlib
import inspect
import re
from collections import Counter
from functools import lru_cache

import jieba

jieba.setLogLevel(30)

# 文档词频缓存条目上限：三个渠道 × 数千片段，每条是一篇文档的 Counter。
DOCUMENT_TERMS_CACHE_SIZE = 50000

_TOKEN_PATTERN = re.compile(r"[\w\u4e00-\u9fff]")


def tokens(text: str) -> list[str]:
    return [t.lower() for t in jieba.cut_for_search(text) if _TOKEN_PATTERN.search(t)]


@lru_cache(maxsize=DOCUMENT_TERMS_CACHE_SIZE)
def document_terms(text: str) -> tuple[Counter, int]:
    """文档的词频与总词数。按文本内容缓存，文本变了自然是新键；返回值只读，调用方不得修改。"""
    terms = Counter(tokens(text))
    return terms, sum(terms.values())


def tokenizer_version() -> str:
    """分词器身份指纹：jieba 版本 + 词典规模 + 本模块实现指纹。

    词典内容摘要没有稳定 API（jieba 惰性加载、可动态加词）；用词典规模近似并
    叠加实现源码指纹，任何一侧变化都会拒绝混用旧派生索引。先强制完成词典初始化，
    保证惰性加载前后指纹一致。
    """
    jieba.initialize()
    return hashlib.sha1(
        f"{jieba.__version__}|{len(jieba.dt.FREQ)}|"
        f"{hashlib.sha1(inspect.getsource(tokens).encode()).hexdigest()}".encode()
    ).hexdigest()[:16]

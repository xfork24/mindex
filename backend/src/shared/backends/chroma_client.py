"""有界 Chroma HTTP 客户端：保留 SDK 集合/校验协议，连接归上下文所有。"""

from __future__ import annotations

import httpx

from shared.query_budget import http_request, remaining


class BudgetTransport(httpx.BaseTransport):
    def __init__(self, timeout, *, verify=True):
        self.timeout, self.verify = timeout, verify
        self.transport = httpx.HTTPTransport(verify=verify)

    def handle_request(self, request):
        if remaining() is None:
            request.extensions["timeout"] = {
                key: self.timeout for key in ("connect", "read", "write", "pool")
            }
            return self.transport.handle_request(request)
        response = http_request(
            request.method,
            str(request.url),
            headers=request.headers,
            content=request.read(),
            timeout=remaining(self.timeout),
            verify=self.verify,
            trust_env=False,
        )
        return httpx.Response(
            response.status_code, headers=response.headers, content=response.content, request=request
        )

    def close(self):
        self.transport.close()


class ChromaClient:
    def __init__(self, settings, timeout):
        # 延迟导入：媒体、数值或服务运行依赖仅在实际执行该功能时加载。
        from chromadb.api import ServerAPI
        from chromadb.config import System

        self.system = System(settings)
        self.api = self.system.instance(ServerAPI)
        # 直接持有 SDK ServerAPI，避免 HttpClient 构造期无期限的租户探测和全局客户端缓存。
        headers = self.api._session.headers
        self.api._session.close()
        self.api._session = httpx.Client(headers=headers, transport=BudgetTransport(timeout))
        self.system.start()
        self.closed = False

    def close(self):
        if not self.closed:
            self.closed = True
            self.api._session.close()
            self.system.stop()

    def heartbeat(self):
        return self.api.heartbeat()

    def _collection(self, method, name, *, embedding_function=None, **kwargs):
        # 延迟导入：媒体、数值或服务运行依赖仅在实际执行该功能时加载。
        from chromadb.api.models.Collection import Collection

        return Collection(
            client=self.api,
            model=getattr(self.api, method)(name=name, **kwargs),
            embedding_function=embedding_function,
        )

    def get_collection(self, name, **kwargs):
        return self._collection("get_collection", name, **kwargs)

    def create_collection(self, name, **kwargs):
        return self._collection("create_collection", name, **kwargs)

    def get_or_create_collection(self, name, **kwargs):
        return self._collection("get_or_create_collection", name, **kwargs)

    def delete_collection(self, name):
        return self.api.delete_collection(name)

    def list_collections(self, **kwargs):
        return self.api.list_collections(**kwargs)

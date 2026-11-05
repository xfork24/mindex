"""业务 API 到单实例助手网关的流式转发，覆盖 SSE、附件与 MCP。"""
import anyio
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

_HOP_HEADERS = {"host", "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
                "te", "trailer", "transfer-encoding", "upgrade", "x-mindex-internal"}


async def forward(request, gateway):
    if not gateway or request.headers.get("x-mindex-gateway-hop"):
        return JSONResponse(status_code=503, content={"detail": "智能助手网关未配置或转发路由形成循环",
                                                      "gateway_url": gateway})
    import httpx

    url = gateway.rstrip("/") + request.url.path
    if request.url.query:
        url += "?" + request.url.query
    headers = {key: value for key, value in request.headers.items() if key.lower() not in _HOP_HEADERS}
    headers["x-mindex-gateway-hop"] = "1"
    client = httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(15, read=None), follow_redirects=False)
    try:
        upstream = await client.send(client.build_request(
            request.method, url, headers=headers, content=request.stream()), stream=True)
    except httpx.HTTPError:
        with anyio.CancelScope(shield=True):
            await client.aclose()
        return JSONResponse(status_code=503, content={"detail": "智能助手网关暂不可用，请稍后重试",
                                                      "gateway_url": gateway})

    except BaseException:
        with anyio.CancelScope(shield=True):
            await client.aclose()
        raise

    async def close():
        with anyio.CancelScope(shield=True):
            try:
                await upstream.aclose()
            finally:
                await client.aclose()

    async def body():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await close()

    return StreamingResponse(body(), status_code=upstream.status_code, background=BackgroundTask(close),
                             headers={key: value for key, value in upstream.headers.items()
                                      if key.lower() not in _HOP_HEADERS})

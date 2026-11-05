"""HTTP manifest 适配器：提交 → 轮询 → 取产物地址（见 ``registry.HttpConfig``）。

请求体：``{"tool": 名称, "arguments": {...}, "uploads": [{"name", "mime", "sha256", "base64"}]}``；
请求头按登记设置（``secret:名称`` 已解引用）。轮询之间检查构建取消，总时长不超过工具的 timeout。
"""

from __future__ import annotations

import time
from urllib.parse import quote, urljoin

from mindex.studio import studio

from .artifacts import Artifact


def dig(value, path: str):
    """点分路径取值（``output.url``、``data.0.url``）；取不到返回 None。"""
    for part in [item for item in path.split(".") if item]:
        if isinstance(value, dict):
            value = value.get(part)
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return None
    return value


def artifact_urls(value, base_url: str) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if isinstance(value, dict):
        value = [value]
    found = []
    for item in value or []:
        if isinstance(item, dict):
            item = item.get("url")
        if isinstance(item, str) and item.strip():
            found.append(urljoin(base_url + "/", item.strip()))
    return found


class HttpAdapter:
    def __init__(self, connection: dict):
        self.cfg = connection
        self.base = connection["base_url"].rstrip("/")

    def _client(self, timeout: float):
        import httpx

        return httpx.Client(timeout=timeout, headers=self.cfg.get("headers") or {}, follow_redirects=False)

    def _json(self, response, noun: str):
        if response.status_code >= 400:
            detail = response.text[:300]
            raise studio.StudioError(f"{noun}失败：HTTP {response.status_code} {detail}")
        try:
            return response.json()
        except ValueError as exc:
            raise studio.StudioError(f"{noun}返回的不是 JSON") from exc

    def list_tools(self, *, timeout: float = 15) -> list[dict] | None:
        """manifest 设置了 tools_path 时列出服务端工具；否则只测试 base_url 可达并返回 None。"""
        import httpx

        try:
            with self._client(timeout) as client:
                if not self.cfg.get("tools_path"):
                    response = client.get(self.base + "/")
                    if response.status_code >= 500:
                        raise studio.StudioError(f"连接失败：HTTP {response.status_code}")
                    return None
                data = self._json(client.get(self.base + self.cfg["tools_path"]), "列出工具")
        except httpx.HTTPError as exc:
            raise studio.StudioError(f"连接失败：{exc}") from exc
        items = data.get("tools") if isinstance(data, dict) else data
        return [{"name": str(item.get("name")), "description": item.get("description")}
                for item in items or [] if isinstance(item, dict) and item.get("name")]

    def call(self, tool: str, arguments: dict, uploads: list[dict], *, timeout: float,
             cancelled=lambda: False, notify=lambda status: None) -> tuple[list[Artifact], dict]:
        """返回 ``(产物列表, 记录)``；记录含任务 ID、最后状态与轮询次数（不含请求头）。"""
        import httpx

        cfg = self.cfg
        deadline = time.monotonic() + timeout
        record = {"task_id": None, "status": None, "polls": 0}
        body = {"tool": tool, "arguments": arguments, "uploads": uploads}
        try:
            with self._client(min(timeout, 120)) as client:
                from mindex.shared.external_calls import call_once

                result = call_once("tool.submit", [self.base, cfg["submit_path"], body],
                    lambda: self._json(client.post(self.base + cfg["submit_path"].replace("{tool}", quote(tool, safe="")),
                                                  json=body), "提交工具任务"),
                    request_id=lambda value: str(dig(value, cfg["id_field"])) if cfg.get("id_field") else None)
                status = dig(result, cfg["status_field"])
                if cfg.get("poll_path") and str(status) not in cfg["done"]:
                    task = dig(result, cfg["id_field"])
                    if task in (None, ""):
                        raise studio.StudioError(f"工具任务没有返回任务 ID（{cfg['id_field']}）")
                    record["task_id"] = str(task)
                    path = cfg["poll_path"].replace("{id}", quote(str(task), safe=""))
                    while True:
                        if str(status) in cfg["failed"]:
                            break
                        if str(status) in cfg["done"]:
                            break
                        if time.monotonic() >= deadline:
                            raise studio.StudioError(f"工具调用超时：{timeout:g} 秒内未完成")
                        end = min(deadline, time.monotonic() + cfg["poll_interval"])
                        while time.monotonic() < end:
                            if cancelled():
                                raise studio.StudioError("已取消")
                            time.sleep(min(0.1, max(0.0, end - time.monotonic())))
                        result = self._json(client.get(self.base + path), "查询工具任务")
                        record["polls"] += 1
                        status = dig(result, cfg["status_field"])
                        notify(status)
                record["status"] = status
                if str(status) in cfg["failed"]:
                    error = dig(result, cfg["error_field"])
                    raise studio.StudioError(f"工具任务失败：{error or status}")
        except httpx.TimeoutException as exc:
            raise studio.StudioError(f"工具调用超时：{exc}") from exc
        except httpx.HTTPError as exc:
            raise studio.StudioError(f"工具调用失败：{exc}") from exc
        urls = artifact_urls(dig(result, cfg["result_field"]), self.base)
        if not urls:
            raise studio.StudioError(f"工具结果中没有产物地址（{cfg['result_field']}）")
        return [Artifact(url=url) for url in urls], record

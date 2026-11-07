"""第三方工具源登记：模型、``tool_sources`` 表存取、白名单检查、密钥引用与配置摘要。

- 一个工具源（Tool_Source）是一个外部服务：协议 ``http``（按 manifest 提交 / 轮询 / 取结果）、``mcp-stdio``
  （在本机以绝对路径启动 MCP 服务器进程）或 ``mcp-http``（streamable-http 的 MCP 服务器）。
- 只有白名单（``tools``）里的工具能被分镜调用；每个工具声明产物类型（video/image）、是否允许携带素材上传
  （``allow_upload``）、超时与产物大小上限。产物只能从 ``artifact_hosts``（http 协议另含 base_url 的主机）
下载。
- 密钥：请求头与环境变量的值写成 ``secret:名称`` 时，运行时替换为该工具源 ``secrets`` 中同名的值。
密钥只写不读：
  接口只返回名称，备份导出（不含密钥时）清空 ``secret`` 列。
- ``tool_hash`` 只覆盖决定产物的配置（协议、连接、工具的产物类型与上传许可），不含密钥值、超时与主机白名单，
  轮换密钥不会让已生成的产物过期。
"""

from __future__ import annotations

import ipaddress
import json
import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from admin.curation import EntityNotFound
from shared.config import StrictModel, fingerprint
from studio import studio
from studio.studio import TOOL_NAME, TOOL_SOURCE_ID
from studio.tools.mcp import unavailable

PROTOCOLS = ("http", "mcp-stdio", "mcp-http")
SECRET_VALUE = re.compile(r"^secret:([A-Za-z0-9_-]{1,40})$")
SECRET_NAME = r"^[A-Za-z0-9_-]{1,40}$"
HOST = re.compile(r"^[A-Za-z0-9.-]{1,253}(:\d{1,5})?$|^\[[0-9a-fA-F:]+\](:\d{1,5})?$")
TOOL_VERSION = 1


class ToolInUse(ValueError):
    """工具源被方案引用（API 返回 409）。"""


def _url(value: str, noun: str) -> str:
    value = value.strip()
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"{noun}须为 http(s) 地址")
    return value.rstrip("/")


class ToolEntry(StrictModel):
    """白名单中的一个工具。"""

    name: str = Field(pattern=TOOL_NAME)
    label: str | None = Field(default=None, max_length=60)
    description: str | None = Field(default=None, max_length=500)
    output: Literal["video", "image"] = "video"
    allow_upload: bool = False
    timeout: int = Field(default=600, ge=5, le=3600, description="单次调用的总超时（秒）")
    max_mb: int = Field(default=500, ge=1, le=2000, description="产物大小上限（MB）")


class HttpConfig(StrictModel):
    """HTTP manifest：``POST base_url + submit_path``（``{tool}`` 替换为工具名）提交 JSON
    ``{"tool", "arguments", "uploads"}``；``poll_path`` 为空时提交响应即结果，否则按 ``id_field`` 取任务 ID，
    轮询 ``GET base_url + poll_path``（``{id}`` 替换）直到 ``status_field`` 进入 done/failed。
    产物地址按 ``result_field``（点分路径）从结果中取：字符串、字符串列表或带 ``url`` 的对象。"""

    base_url: str
    headers: dict[str, str] = Field(default_factory=dict)
    submit_path: str = Field(default="/tools/{tool}/jobs", max_length=300)
    poll_path: str | None = Field(default="/jobs/{id}", max_length=300)
    poll_interval: float = Field(default=2.0, ge=0.05, le=60)
    id_field: str = Field(default="id", max_length=100)
    status_field: str = Field(default="status", max_length=100)
    done: list[str] = Field(
        default_factory=lambda: ["succeeded", "completed", "done", "success"], max_length=10
    )
    failed: list[str] = Field(default_factory=lambda: ["failed", "error", "cancelled"], max_length=10)
    result_field: str = Field(default="output.url", max_length=100)
    error_field: str = Field(default="error", max_length=100)
    tools_path: str | None = Field(
        default=None, max_length=300, description="GET 列出工具（可选，用于测试连接与发现）"
    )

    _base = field_validator("base_url")(classmethod(lambda cls, v: _url(v, "base_url")))

    @field_validator("submit_path", "poll_path", "tools_path")
    @classmethod
    def _path(cls, value):
        if value is not None and not value.startswith("/"):
            raise ValueError("路径须以 / 开头")
        return value


class StdioConfig(StrictModel):
    """本机 MCP 服务器进程：``command`` 必须是绝对路径（桌面版不依赖 GUI 的 PATH）；``env``
    在最小默认环境上追加。
    """

    command: str = Field(min_length=2, max_length=1000)
    args: list[str] = Field(default_factory=list, max_length=30)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = Field(default=None, max_length=1000)

    @field_validator("command", "cwd")
    @classmethod
    def _absolute(cls, value):
        if value is not None and not value.startswith("/"):
            raise ValueError("须为绝对路径（例如 /usr/local/bin/my-mcp-server）")
        return value


class McpHttpConfig(StrictModel):
    url: str
    headers: dict[str, str] = Field(default_factory=dict)

    _url = field_validator("url")(classmethod(lambda cls, v: _url(v, "url")))


class ToolSourceSpec(StrictModel):
    """登记或整体替换一个工具源；``secrets`` 只写：给出的名称覆盖（值为 null 删除），未给出的保留。"""

    name: str = Field(min_length=1, max_length=60)
    protocol: Literal["http", "mcp-stdio", "mcp-http"]
    http: HttpConfig | None = None
    stdio: StdioConfig | None = None
    mcp_http: McpHttpConfig | None = None
    tools: list[ToolEntry] = Field(min_length=1, max_length=50)
    artifact_hosts: list[str] = Field(default_factory=list, max_length=20)
    secrets: dict[str, str | None] | None = Field(default=None, description="密钥（只写）：名称 → 值")

    @field_validator("artifact_hosts")
    @classmethod
    def _hosts(cls, value):
        cleaned = []
        for item in value:
            item = item.strip().lower()
            if not HOST.fullmatch(item):
                raise ValueError(f"产物主机须为主机名（可带端口），不含协议与路径：{item}")
            if item not in cleaned:
                cleaned.append(item)
        return cleaned

    @field_validator("secrets")
    @classmethod
    def _secret_names(cls, value):
        for key in value or {}:
            if not re.fullmatch(SECRET_NAME, key):
                raise ValueError(f"密钥名称只能包含字母、数字、下划线与连字符：{key}")
        return value

    @model_validator(mode="after")
    def _connection(self):
        wanted = {"http": "http", "mcp-stdio": "stdio", "mcp-http": "mcp_http"}[self.protocol]
        for key in ("http", "stdio", "mcp_http"):
            if (getattr(self, key) is not None) != (key == wanted):
                raise ValueError(f"协议 {self.protocol} 需要且只需要 {wanted} 连接设置")
        names = [tool.name for tool in self.tools]
        if len(set(names)) != len(names):
            raise ValueError("工具白名单中有重复的工具名")
        return self

    def connection(self) -> dict:
        key = {"http": "http", "mcp-stdio": "stdio", "mcp-http": "mcp_http"}[self.protocol]
        return getattr(self, key).model_dump()


def secret_names(connection: dict) -> set[str]:
    """连接设置中引用的密钥名称（请求头与环境变量）。"""
    found = set()
    for group in ("headers", "env"):
        for value in (connection.get(group) or {}).values():
            if match := SECRET_VALUE.fullmatch(value or ""):
                found.add(match.group(1))
    return found


def resolve(connection: dict, secrets: dict[str, str]) -> dict:
    """把连接设置中的 ``secret:名称`` 替换为密钥值；缺少密钥时报错。"""

    result = json.loads(json.dumps(connection))
    for group in ("headers", "env"):
        for key, value in (result.get(group) or {}).items():
            if match := SECRET_VALUE.fullmatch(value or ""):
                name = match.group(1)
                if not secrets.get(name):
                    raise studio.StudioError(f"工具源缺少密钥 {name}，请在第三方工具设置里填写")
                result[group][key] = secrets[name]
    return result


# ---------------------------------------------------------------- 存取


def _row(store, source_id: str) -> dict:
    row = (
        store.one("SELECT * FROM tool_sources WHERE id=?", (source_id,))
        if re.fullmatch(TOOL_SOURCE_ID, source_id)
        else None
    )
    if row is None:
        raise EntityNotFound(f"找不到第三方工具源：{source_id}")
    return row


def load(row: dict) -> dict:
    """数据库行 → 运行用的完整记录（含密钥）。"""
    return {
        "id": row["id"],
        "name": row["name"],
        "protocol": row["protocol"],
        "connection": json.loads(row["connection"]),
        "tools": json.loads(row["tools"]),
        "artifact_hosts": json.loads(row["artifact_hosts"]),
        "secrets": json.loads(row["secret"] or "{}"),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def get_source(store, source_id: str) -> dict:
    return load(_row(store, source_id))


def view(record: dict) -> dict:
    """接口视图：密钥只给名称与是否已填写。"""
    referenced = secret_names(record["connection"])
    return {
        key: record[key]
        for key in (
            "id",
            "name",
            "protocol",
            "connection",
            "tools",
            "artifact_hosts",
            "created_at",
            "updated_at",
        )
    } | {
        "secrets": [
            {"name": name, "set": bool(record["secrets"].get(name))}
            for name in sorted(set(record["secrets"]) | referenced)
        ],
        "missing_secrets": sorted(name for name in referenced if not record["secrets"].get(name)),
    }


def list_sources(store) -> list[dict]:
    return [view(load(row)) for row in store.rows("SELECT * FROM tool_sources ORDER BY created_at, id")]


def save_source(store, source_id: str, spec: ToolSourceSpec, *, now: str) -> dict:
    """登记或整体替换工具源；返回视图。"""

    if not re.fullmatch(TOOL_SOURCE_ID, source_id):
        raise studio.StudioError("工具源 ID 只能由小写字母、数字与连字符组成（≤40 字符）")
    existing = store.one("SELECT * FROM tool_sources WHERE id=?", (source_id,))
    secrets = json.loads(existing["secret"] or "{}") if existing else {}
    for key, value in (spec.secrets or {}).items():
        if value:
            secrets[key] = value
        else:
            secrets.pop(key, None)
    values = (
        spec.name,
        spec.protocol,
        json.dumps(spec.connection(), ensure_ascii=False),
        json.dumps([tool.model_dump() for tool in spec.tools], ensure_ascii=False),
        json.dumps(spec.artifact_hosts),
        json.dumps(secrets) if secrets else None,
    )
    if existing:
        store.execute(
            "UPDATE tool_sources SET name=?,protocol=?,connection=?,tools=?,artifact_hosts=?,secret=?,"
            "updated_at=? WHERE id=?",
            (*values, now, source_id),
        )
    else:
        store.execute(
            "INSERT INTO tool_sources(id,name,protocol,connection,tools,artifact_hosts,secret,created_at,"
            "updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (source_id, *values, now, now),
        )
    return view(get_source(store, source_id))


def references(config, source_id: str) -> list[dict]:
    """方案所选分镜内容中引用某工具源的分镜（扫描全部 workspace.json，只读）。"""

    root = studio.studio_root(config)
    found = []
    if not root.is_dir():
        return found
    for path in sorted(root.glob("*/workspace.json")):
        try:
            w = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        versions = w.get("versions") or {}
        for plan in (w.get("plans") or {}).values():
            for sid, selected in (plan.get("shots") or {}).items():
                data = (versions.get((selected or {}).get("content") or "") or {}).get("data") or {}
                source = data.get("source") or {}
                if source.get("kind") == "tool" and source.get("tool_id") == source_id:
                    found.append(
                        {
                            "job_id": path.parent.name,
                            "plan_id": plan.get("id"),
                            "plan_name": plan.get("name"),
                            "shot_id": sid,
                        }
                    )
    return found


def delete_source(config, store, source_id: str) -> None:
    _row(store, source_id)
    used = references(config, source_id)
    if used:
        first = used[0]
        raise ToolInUse(
            f"工具源正被 {len(used)} 处分镜使用（如任务 {first['job_id']} 的方案「{first['plan_name']}」），"
            "请先把这些分镜改用其他画面来源"
        )
    store.execute("DELETE FROM tool_sources WHERE id=?", (source_id,))


# ---------------------------------------------------------------- 调用前检查


def tool_entry(record: dict, name: str) -> dict:

    for tool in record["tools"]:
        if tool["name"] == name:
            return tool
    raise studio.StudioError(f"工具 {name} 不在工具源「{record['name']}」的白名单中")


def tool_hash(record: dict, name: str) -> str:
    """决定产物的工具配置摘要（进入 invoke 签名）。"""
    tool = tool_entry(record, name)
    return fingerprint(
        {
            "protocol": record["protocol"],
            "connection": record["connection"],
            "tool": {key: tool[key] for key in ("name", "output", "allow_upload")},
            "tool_version": TOOL_VERSION,
        }
    )


def blocker(store, source: dict) -> str | None:
    """分镜工具来源的阻塞原因（预检与执行前）：未登记、不在白名单、产物类型不符、上传未授权、
    缺密钥、缺 mcp 组件。
    """

    try:
        record = get_source(store, source["tool_id"])
        tool = tool_entry(record, source["tool"])
    except (EntityNotFound, studio.StudioError) as exc:
        return str(exc)
    if tool["output"] != source.get("output", "video"):
        return (
            f"工具 {tool['name']} 的产物是{'视频' if tool['output'] == 'video' else '图片'}，与分镜设置不符"
        )
    if source.get("uploads") and not tool["allow_upload"]:
        return (
            f"工具 {tool['name']} 未允许携带素材上传，请在第三方工具设置里勾选允许上传，或去掉分镜的上传素材"
        )
    if missing := sorted(
        name for name in secret_names(record["connection"]) if not record["secrets"].get(name)
    ):
        return f"工具源「{record['name']}」缺少密钥：{'、'.join(missing)}"
    if record["protocol"] != "http":
        if reason := unavailable():
            return reason
    return None


def allowed_hosts(record: dict) -> set[str]:
    """产物下载允许的主机：``artifact_hosts`` 加上 http 协议的 base_url 主机（含端口）。"""
    hosts = set(record["artifact_hosts"])
    if record["protocol"] == "http":
        hosts.add(urlsplit(record["connection"]["base_url"]).netloc.lower())
    return hosts


def is_private(host: str) -> bool:
    try:
        return ipaddress.ip_address(host.strip("[]")).is_private
    except ValueError:
        return host in ("localhost",)
